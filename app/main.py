import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from typing import Any, Dict
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.services.analyze import analyze_site
from app.services.checklist import generate_checklist
from app.services.condition_matrix import build_condition_matrix
from app.services.flow_map import build_flow_map
from app.services.flows import finalize_flows, run_flows
from app.services.page_audit import auto_checklist_from_sitemap
from app.services.final_output import write_final_testsheet
from app.services.execute_checklist import build_execution_graph, execute_checklist_rows
from app.services.storage import delete_bundle, get_bundle, migrate, save_analysis, save_flows
from app.services.structure_map import build_structure_map
from app.services.state_transition import run_transition_check
from app.services.qa_templates import build_template_steps, list_templates
from app.services.user_signup import attempt_user_signup
from app.services.google_sheets import audit_log, pull_and_validate
from app.services.geo_audit import run_geo_audit

APP_NAME = "qa-mvp-fastapi"
NODE_API_BASE = os.getenv("QA_NODE_API_BASE", "http://127.0.0.1:4173").rstrip("/")
WEB_ORIGIN = os.getenv("QA_WEB_ORIGIN", "*").strip() or "*"
REQUEST_TIMEOUT_SEC = float(os.getenv("QA_API_TIMEOUT_SEC", "180"))
HEALTH_UPSTREAM_TIMEOUT_SEC = float(os.getenv("QA_HEALTH_UPSTREAM_TIMEOUT_SEC", "2.5"))
AUTH_STORE_PATH = Path("out/auth-profiles.json")
GEO_TEST_PAGE_PATH = Path(__file__).with_name("static").joinpath("geo-test.html")

logger = logging.getLogger(APP_NAME)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    migrate()
    yield


app = FastAPI(title=APP_NAME, version="0.1.0", lifespan=_lifespan)

native_analysis_store: Dict[str, Dict[str, Any]] = {}
execute_jobs: Dict[str, Dict[str, Any]] = {}
Path("out").mkdir(parents=True, exist_ok=True)
app.mount("/out", StaticFiles(directory="out"), name="out")


@app.middleware("http")
async def _catch_unhandled_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("unhandled error: %s %s", request.method, request.url.path)
        raise HTTPException(
            status_code=500,
            detail=_error_detail("server", "UNHANDLED_EXCEPTION", "서버 내부 오류가 발생했습니다.", str(e)),
        ) from e


async def _json_payload(req: Request) -> Dict[str, Any]:
    try:
        data = await req.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("config", "INVALID_JSON", "요청 본문(JSON) 형식이 올바르지 않습니다.", str(e)),
        ) from e
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail=_error_detail("config", "JSON_OBJECT_REQUIRED", "JSON 객체 형태의 본문이 필요합니다."),
        )
    return data

allow_origins = ["*"] if WEB_ORIGIN == "*" else [WEB_ORIGIN]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_llm(payload: Dict[str, Any]) -> tuple[Any, Any, Dict[str, Any]]:
    provider = payload.get("llmProvider")
    model = (str(payload.get("llmModel", "")).strip() or None)
    llm_auth = payload.get("llmAuth") if isinstance(payload.get("llmAuth"), dict) else {}
    providers = payload.get("llmProviders") if isinstance(payload.get("llmProviders"), list) else []
    routing = payload.get("llmRouting") if isinstance(payload.get("llmRouting"), dict) else {}
    r_providers = routing.get("providers") if isinstance(routing.get("providers"), list) else []
    if r_providers:
        provider = ",".join([str(x).strip() for x in r_providers if str(x).strip()])
    elif providers:
        provider = ",".join([str(x).strip() for x in providers if str(x).strip()])
    r_auth = routing.get("auth") if isinstance(routing.get("auth"), dict) else {}
    if r_auth:
        llm_auth = {**llm_auth, **r_auth}
    # merge saved auth profile (OpenClaw-like)
    saved_openai = _get_profile_auth("openai")
    if saved_openai:
        current_openai = llm_auth.get("openai") if isinstance(llm_auth.get("openai"), dict) else {}
        llm_auth["openai"] = {**saved_openai, **current_openai}
    return provider, model, llm_auth


def _save_native_bundle(analysis_id: str, base_url: str, pages: list[dict[str, Any]], elements: list[dict[str, Any]], candidates: list[dict[str, Any]], reports: Dict[str, Any] | None = None, auth: Dict[str, Any] | None = None) -> None:
    bundle = {
        "analysis": {"analysisId": analysis_id, "baseUrl": base_url},
        "pages": pages,
        "elements": elements,
        "candidates": candidates,
        "reports": reports or {},
        "auth": auth or {},
        "createdAt": int(time.time()),
    }
    native_analysis_store[analysis_id] = bundle
    save_analysis(analysis_id, base_url, pages, elements, candidates)


def _load_bundle(analysis_id: str) -> Dict[str, Any] | None:
    if analysis_id in native_analysis_store:
        return native_analysis_store[analysis_id]
    db = get_bundle(analysis_id)
    if db:
        native_analysis_store[analysis_id] = db
    return db


def _safe_unlink(path: str) -> bool:
    p = Path(path or "")
    if not p.exists() or not p.is_file():
        return False
    out_root = Path("out").resolve()
    target = p.resolve()
    if out_root == target or out_root not in target.parents:
        return False
    try:
        p.unlink()
        return True
    except Exception:
        return False


def _cleanup_entities(analysis_ids: list[str], job_ids: list[str], artifact_paths: list[str] | None = None) -> Dict[str, Any]:
    artifact_paths = artifact_paths or []
    deleted_analysis = []
    deleted_jobs = []
    deleted_artifacts = []

    for analysis_id in analysis_ids:
        analysis_id = str(analysis_id or "").strip()
        if not analysis_id:
            continue
        in_mem = native_analysis_store.pop(analysis_id, None) is not None
        in_db = delete_bundle(analysis_id)
        if in_mem or in_db:
            deleted_analysis.append(analysis_id)

    for job_id in job_ids:
        job_id = str(job_id or "").strip()
        if not job_id:
            continue
        if execute_jobs.pop(job_id, None) is not None:
            deleted_jobs.append(job_id)

    for path in artifact_paths:
        ap = str(path or "").strip()
        if ap and _safe_unlink(ap):
            deleted_artifacts.append(ap)

    return {
        "analysisIds": deleted_analysis,
        "jobIds": deleted_jobs,
        "artifacts": deleted_artifacts,
    }


async def proxy_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{NODE_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=payload)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=_error_detail("network", "UPSTREAM_UNAVAILABLE", "상위 서비스 연결에 실패했습니다.", str(e)),
        ) from e

    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except Exception:
            detail = {"error": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


async def proxy_get(path: str) -> Dict[str, Any]:
    url = f"{NODE_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC) as client:
            resp = await client.get(url)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=_error_detail("network", "UPSTREAM_UNAVAILABLE", "상위 서비스 연결에 실패했습니다.", str(e)),
        ) from e

    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except Exception:
            detail = {"error": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=detail)

    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "text": resp.text[:500]}

def _load_auth_profiles() -> Dict[str, Any]:
    try:
        if AUTH_STORE_PATH.exists():
            return json.loads(AUTH_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_auth_profiles(data: Dict[str, Any]) -> None:
    AUTH_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _pkce_challenge(verifier: str) -> str:
    h = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(h).decode().rstrip("=")


def _get_profile_auth(provider: str) -> Dict[str, Any]:
    profiles = _load_auth_profiles()
    p = profiles.get(provider) if isinstance(profiles.get(provider), dict) else {}
    return p


def _error_detail(category: str, code: str, user_message: str, debug_detail: Any = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "errorCategory": category,
        "errorCode": code,
        "userMessage": user_message,
        "debugDetail": debug_detail,
    }


def _decision_hint(summary: Dict[str, Any]) -> str:
    fail = int(summary.get("FAIL") or 0)
    blocked = int(summary.get("BLOCKED") or 0)
    if fail > 0:
        return "hold"
    if blocked > 0:
        return "ship_with_caution"
    return "ship"


def _build_final_summary(summary: Dict[str, Any], hints: Dict[str, str] | None = None) -> Dict[str, Any]:
    hints = hints or {}
    blocker_items = [{"code": k, "message": v} for k, v in hints.items()][:5]
    decision = _decision_hint(summary)
    actions: list[str] = []
    if summary.get("FAIL"):
        actions.append("실패 항목을 우선 수정하고 재실행하세요.")
    if summary.get("BLOCKED"):
        actions.append("BLOCKED 항목의 사전 조건(권한/데이터)을 확인하세요.")
    if not actions:
        actions.append("경고 항목 위주로 최종 점검 후 배포하세요.")
    return {
        "critical_fail_count": int(summary.get("FAIL") or 0),
        "warning_count": int(summary.get("BLOCKED") or 0),
        "blockers_top": blocker_items,
        "action_items": actions[:3],
        "decision_hint": decision,
    }


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": APP_NAME,
        "nodeApiBase": NODE_API_BASE,
    }


@app.get("/geo-test")
async def geo_test_page() -> FileResponse:
    if not GEO_TEST_PAGE_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=_error_detail("server", "GEO_TEST_PAGE_MISSING", "geo-test page is missing", str(GEO_TEST_PAGE_PATH)),
        )
    return FileResponse(GEO_TEST_PAGE_PATH)


@app.post("/api/geo-audit")
async def geo_audit(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("config", "URL_REQUIRED", "url is required"),
        )

    try:
        return await run_geo_audit(url)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("config", "INVALID_URL", "invalid url", str(e)),
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=502,
            detail=_error_detail("dependency", "GEO_CRAWL_FAILED", "failed to crawl target url", str(e)),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=_error_detail("server", "GEO_AUDIT_FAILED", "geo audit failed", str(e)),
        ) from e


@app.get("/health")
async def health() -> Dict[str, Any]:
    upstream_ok = False
    upstream_detail: Any = None
    try:
        async with httpx.AsyncClient(timeout=HEALTH_UPSTREAM_TIMEOUT_SEC) as client:
            resp = await client.get(f"{NODE_API_BASE}/")
            upstream_ok = resp.status_code < 500
            try:
                upstream_detail = resp.json()
            except Exception:
                upstream_detail = {"status": resp.status_code, "text": resp.text[:200]}
    except Exception as e:
        upstream_detail = str(e)

    return {
        "ok": True,
        "service": APP_NAME,
        "upstream": NODE_API_BASE,
        "upstreamOk": upstream_ok,
        "upstreamDetail": upstream_detail,
    }


@app.post("/api/sheets/pull")
async def sheets_pull(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    sheets = payload.get("sheets") if isinstance(payload.get("sheets"), list) else None
    strict = bool(payload.get("strict", False))
    try:
        out = pull_and_validate(sheets=sheets)
    except Exception as e:
        audit_log("google_sheets_pull_failed", {"error": str(e)})
        raise HTTPException(status_code=400, detail={"ok": False, "error": str(e)}) from e

    if strict and int(out.get("summary", {}).get("totalErrors", 0)) > 0:
        audit_log("google_sheets_pull_strict_reject", {"summary": out.get("summary")})
        raise HTTPException(status_code=422, detail=out)
    return out


@app.get("/api/sheets/pull")
async def sheets_pull_get() -> Dict[str, Any]:
    try:
        return pull_and_validate()
    except Exception as e:
        audit_log("google_sheets_pull_failed", {"error": str(e)})
        raise HTTPException(status_code=400, detail={"ok": False, "error": str(e)}) from e


@app.post("/api/llm/oauth/start")
async def llm_oauth_start(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req) if req.method else {}
    provider = str((payload or {}).get("provider") or "openai").strip().lower()
    if provider != "openai":
        raise HTTPException(status_code=400, detail={"ok": False, "error": "only openai supported"})

    client_id = os.getenv("QA_OPENAI_OAUTH_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("QA_OPENAI_OAUTH_REDIRECT_URI", "").strip()
    auth_url = os.getenv("QA_OPENAI_OAUTH_AUTH_URL", "https://auth.openai.com/oauth/authorize").strip()
    scope = os.getenv("QA_OPENAI_OAUTH_SCOPE", "openid profile offline_access").strip()
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "QA_OPENAI_OAUTH_CLIENT_ID/REDIRECT_URI required"})

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_challenge(verifier)

    profiles = _load_auth_profiles()
    pending = profiles.get("_pending") if isinstance(profiles.get("_pending"), dict) else {}
    pending[state] = {"provider": provider, "verifier": verifier, "createdAt": int(time.time() * 1000)}
    profiles["_pending"] = pending
    _save_auth_profiles(profiles)

    q = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return {"ok": True, "provider": provider, "authUrl": f"{auth_url}?{q}", "state": state}


@app.get("/api/llm/oauth/callback")
async def llm_oauth_callback(code: str = "", state: str = "", error: str = "") -> Dict[str, Any]:
    if error:
        raise HTTPException(status_code=400, detail={"ok": False, "error": error})
    if not code or not state:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "code/state required"})

    profiles = _load_auth_profiles()
    pending = profiles.get("_pending") if isinstance(profiles.get("_pending"), dict) else {}
    item = pending.get(state) if isinstance(pending.get(state), dict) else {}
    verifier = str(item.get("verifier") or "")
    if not verifier:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "invalid state"})

    client_id = os.getenv("QA_OPENAI_OAUTH_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("QA_OPENAI_OAUTH_REDIRECT_URI", "").strip()
    token_url = os.getenv("QA_OPENAI_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token").strip()
    client_secret = os.getenv("QA_OPENAI_OAUTH_CLIENT_SECRET", "").strip()

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(token_url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code >= 400:
            raise HTTPException(status_code=400, detail={"ok": False, "error": f"token exchange failed {r.status_code}"})
        td = r.json()
        access = str(td.get("access_token") or "")
        if not access:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "access_token missing"})

        profiles["openai"] = {
            "mode": "oauthToken",
            "oauthToken": access,
            "refreshToken": str(td.get("refresh_token") or ""),
            "tokenType": str(td.get("token_type") or "Bearer"),
            "expiresIn": int(td.get("expires_in") or 0),
            "updatedAt": int(time.time() * 1000),
        }
        pending.pop(state, None)
        profiles["_pending"] = pending
        _save_auth_profiles(profiles)
        return {"ok": True, "provider": "openai", "connected": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"ok": False, "error": str(e)})


@app.get("/api/llm/oauth/status")
async def llm_oauth_status(provider: str = "openai") -> Dict[str, Any]:
    p = _get_profile_auth(provider)
    connected = bool((p.get("oauthToken") or p.get("apiKey"))) if isinstance(p, dict) else False
    return {"ok": True, "provider": provider, "connected": connected, "mode": p.get("mode") if isinstance(p, dict) else None, "updatedAt": p.get("updatedAt") if isinstance(p, dict) else None}


@app.post("/api/llm/oauth/logout")
async def llm_oauth_logout(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    provider = str((payload or {}).get("provider") or "openai").strip().lower()
    profiles = _load_auth_profiles()
    profiles.pop(provider, None)
    _save_auth_profiles(profiles)
    return {"ok": True, "provider": provider, "connected": False}


@app.post("/api/analyze")
async def analyze(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    base_url = str(payload.get("baseUrl", "")).strip()
    if not base_url:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "baseUrl required"})

    # Native FastAPI implementation (phase-2 migration target)
    provider, model, llm_auth = _resolve_llm(payload)
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    try:
        result = await analyze_site(base_url, provider=provider, model=model, llm_auth=llm_auth)
        analysis_id = str(result.get("analysisId") or f"py_analysis_{int(time.time() * 1000)}")
        native_pages = result.get("_native", {}).get("pages") or [result.get("_native", {}).get("page", {})]
        _save_native_bundle(
            analysis_id,
            base_url,
            native_pages,
            [],
            result.get("candidates", []),
            reports=result.get("reports", {}),
            auth=auth,
        )
        result["analysisId"] = analysis_id
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail={"ok": False, "error": str(e)}) from e


@app.get("/api/analysis/{analysis_id}")
async def analysis_get(analysis_id: str) -> Dict[str, Any]:
    bundle = _load_bundle(analysis_id)
    if bundle:
        return {
            "ok": True,
            "storage": "fastapi-sqlite",
            "analysis": bundle.get("analysis"),
            "pages": bundle.get("pages", []),
            "elements": bundle.get("elements", []),
            "candidates": bundle.get("candidates", []),
        }
    return await proxy_get(f"/api/analysis/{analysis_id}")


@app.delete("/api/analysis/{analysis_id}")
async def analysis_delete(analysis_id: str) -> Dict[str, Any]:
    cleanup = _cleanup_entities([analysis_id], [])
    return {"ok": True, "analysisId": analysis_id, "deleted": analysis_id in set(cleanup.get("analysisIds") or [])}


@app.post("/api/flow-map")
async def flow_map(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    analysis_id = str(payload.get("analysisId", "")).strip()
    if not analysis_id:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "analysisId required"})

    bundle = _load_bundle(analysis_id)
    if not bundle:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "analysis not found"})

    screen = str(payload.get("screen", "")).strip()
    context = str(payload.get("context", "")).strip()
    return build_flow_map(bundle, screen=screen, context=context)


@app.post("/api/structure-map")
async def structure_map(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    analysis_id = str(payload.get("analysisId", "")).strip()
    if not analysis_id:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "analysisId required"})

    bundle = _load_bundle(analysis_id)
    if not bundle:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "analysis not found"})

    flowmap = build_flow_map(bundle, screen=str(payload.get("screen", "")).strip(), context=str(payload.get("context", "")).strip())
    return build_structure_map(bundle, flowmap)


@app.post("/api/condition-matrix")
async def condition_matrix(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    screen = str(payload.get("screen", "")).strip()
    if not screen:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "screen required"})

    context = str(payload.get("context", "")).strip()
    include_auth = bool(payload.get("includeAuth", True))
    return build_condition_matrix(screen, context=context, include_auth=include_auth)


@app.post("/api/checklist")
async def checklist(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    screen = str(payload.get("screen", "")).strip()
    if not screen:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "screen required"})

    context = str(payload.get("context", "")).strip()
    include_auth = bool(payload.get("includeAuth", False))
    provider, model, llm_auth = _resolve_llm(payload)
    checklist_expand = bool(payload.get("checklistExpand", False))
    checklist_expand_mode = str(payload.get("checklistExpandMode", "none") or "none").strip()
    checklist_expand_limit = int(payload.get("checklistExpandLimit", 40) or 40)

    # Native FastAPI implementation + condition matrix expansion
    out = await generate_checklist(
        screen,
        context,
        include_auth,
        provider=provider,
        model=model,
        llm_auth=llm_auth,
        expand=checklist_expand,
        expand_mode=checklist_expand_mode,
        max_rows=max(6, min(checklist_expand_limit, 300)),
    )
    matrix = build_condition_matrix(screen, context=context, include_auth=include_auth)

    # merge/dedup by 시나리오 text
    merged = []
    seen = set()
    for r in (out.get("rows") or []) + (matrix.get("rows") or []):
        scenario_key = str(r.get("action") or r.get("테스트시나리오") or "").strip()
        expected_key = str(r.get("expected") or r.get("확인") or "").strip()
        k = f"{scenario_key}::{expected_key}"
        if not scenario_key or k in seen:
            continue
        seen.add(k)
        merged.append(r)

    response_limit = max(40, checklist_expand_limit) if checklist_expand else 40
    out["rows"] = merged[:response_limit]
    cols = out.get("columns") or ["화면", "구분", "테스트시나리오", "확인", "module", "element", "action", "expected", "actual"]
    out["tsv"] = "\n".join([
        "\t".join(cols),
        *["\t".join(str(x.get(c, "")) for c in cols) for x in out["rows"]],
    ])
    out["conditionMatrix"] = {
        "surface": matrix.get("surface"),
        "roles": matrix.get("roles"),
        "conditions": matrix.get("conditions"),
        "count": len(matrix.get("rows") or []),
    }

    # coverage-driven missing area hints
    text_all = "\n".join([f"{x.get('action') or ''} {x.get('expected') or ''} {x.get('테스트시나리오') or ''}" for x in out.get("rows") or []]).lower()
    checks = {
        "AUTH": ["권한", "로그인", "비로그인", "접근"],
        "VALIDATION": ["유효성", "필수", "입력", "에러"],
        "INTERACTION": ["클릭", "버튼", "링크", "이동"],
        "RESPONSIVE": ["반응형", "모바일", "해상도"],
        "PUBLISHING": ["레이아웃", "퍼블리싱", "정렬", "간격"],
    }
    missing = []
    for k, kws in checks.items():
        if not any(w in text_all for w in kws):
            missing.append(k)
    out["missingAreas"] = missing
    return out


def _extract_execute_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "rows required"})
    max_rows = max(1, min(int(payload.get("maxRows", 20) or 20), 80))
    return {
        "rows": rows,
        "max_rows": max_rows,
        "auth": payload.get("auth") if isinstance(payload.get("auth"), dict) else {},
        "exhaustive": bool(payload.get("exhaustive", False)),
        "exhaustive_clicks": max(1, min(int(payload.get("exhaustiveClicks", 8) or 8), 16)),
        "exhaustive_inputs": max(1, min(int(payload.get("exhaustiveInputs", 8) or 8), 16)),
        "exhaustive_depth": max(1, min(int(payload.get("exhaustiveDepth", 1) or 1), 2)),
        "exhaustive_budget_ms": max(3000, min(int(payload.get("exhaustiveBudgetMs", 12000) or 12000), 30000)),
        "allow_risky_actions": bool(payload.get("allowRiskyActions", False)),
        "run_id": str(payload.get("runId", "")).strip() or f"exec_{int(time.time()*1000)}",
        "project_name": str(payload.get("projectName", "QA 테스트시트")).strip(),
    }


async def _execute_and_finalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    result = await execute_checklist_rows(
        cfg["rows"],
        max_rows=cfg["max_rows"],
        auth=cfg["auth"],
        exhaustive=cfg["exhaustive"],
        exhaustive_clicks=cfg["exhaustive_clicks"],
        exhaustive_inputs=cfg["exhaustive_inputs"],
        exhaustive_depth=cfg["exhaustive_depth"],
        exhaustive_budget_ms=cfg["exhaustive_budget_ms"],
        allow_risky_actions=cfg["allow_risky_actions"],
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    final_sheet = write_final_testsheet(cfg["run_id"], cfg["project_name"], result.get("rows") or [])
    graph_payload = result.get("executionGraph") or result.get("graph") or build_execution_graph(result.get("rows") or [], result.get("chainStatuses") or {})
    summary = result.get("summary") or {}
    failure_hints = result.get("failureCodeHints") or {}
    return {
        "ok": True,
        "summary": summary,
        "finalSummary": _build_final_summary(summary, failure_hints),
        "coverage": result.get("coverage"),
        "metrics": result.get("metrics") or {},
        "failureCodeHints": failure_hints,
        "retryStats": result.get("retryStats") or {},
        "chainStatuses": result.get("chainStatuses") or {},
        "graph": graph_payload,
        "executionGraph": graph_payload,
        "loginUsed": result.get("loginUsed", False),
        "rows": result.get("rows"),
        "decompositionRows": result.get("decompositionRows") or [],
        "decompositionRowsPath": result.get("decompositionRowsPath") or "",
        "finalSheet": final_sheet,
    }


@app.post("/api/checklist/execute")
async def checklist_execute(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    cfg = _extract_execute_payload(payload)
    out = await _execute_and_finalize(cfg)
    if not out.get("ok"):
        raise HTTPException(status_code=500, detail={"ok": False, "error": out.get("error")})
    return out


@app.post("/api/checklist/execute/async")
async def checklist_execute_async(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    cfg = _extract_execute_payload(payload)
    batch_size = max(1, min(int(payload.get("batchSize", 8) or 8), 12))
    job_id = f"job_{uuid4().hex[:12]}"
    total_rows = len(cfg.get("rows") or [])
    now_ms = int(time.time() * 1000)
    execute_jobs[job_id] = {
        "ok": True,
        "jobId": job_id,
        "status": "queued",
        "createdAt": now_ms,
        "progress": {
            "phase": "queued",
            "doneRows": 0,
            "totalRows": total_rows,
            "completed_rows": 0,
            "target_rows": total_rows,
            "percent": 0,
            "elapsedMs": 0,
            "etaMs": None,
            "lastMessage": "실행 대기 중",
        },
    }

    async def _runner() -> None:
        execute_jobs[job_id]["status"] = "running"
        execute_jobs[job_id]["startedAt"] = int(time.time() * 1000)
        execute_jobs[job_id]["progress"]["phase"] = "execute"
        execute_jobs[job_id]["progress"]["lastMessage"] = "체크리스트 실행 중"
        try:
            rows_all = cfg.get("rows") or []
            merged_rows: list[Dict[str, Any]] = []
            merged_decomp_rows: list[Dict[str, Any]] = []
            merged_summary = {"PASS": 0, "FAIL": 0, "BLOCKED": 0}
            merged_hints: Dict[str, str] = {}
            last_cov: Dict[str, Any] = {}
            merged_retry_stats: Dict[str, Any] = {
                "eligibleRows": 0,
                "ineligibleRows": 0,
                "byClass": {"NONE": 0, "TRANSIENT": 0, "WEAK_SIGNAL": 0, "CONDITIONAL": 0, "NON_RETRYABLE": 0},
            }
            merged_chain_statuses: Dict[str, str] = {}
            merged_metrics: Dict[str, Any] = {"completed_rows": 0, "target_rows": len(rows_all)}

            for i in range(0, len(rows_all), batch_size):
                chunk = rows_all[i:i + batch_size]
                part = await execute_checklist_rows(
                    chunk,
                    max_rows=len(chunk),
                    auth=cfg["auth"],
                    exhaustive=cfg["exhaustive"],
                    exhaustive_clicks=cfg["exhaustive_clicks"],
                    exhaustive_inputs=cfg["exhaustive_inputs"],
                    exhaustive_depth=cfg["exhaustive_depth"],
                    exhaustive_budget_ms=cfg["exhaustive_budget_ms"],
                    allow_risky_actions=cfg["allow_risky_actions"],
                )
                if not part.get("ok"):
                    raise Exception(str(part.get("error") or "execute failed"))

                merged_rows.extend(part.get("rows") or [])
                merged_decomp_rows.extend(part.get("decompositionRows") or [])
                s = part.get("summary") or {}
                merged_summary["PASS"] += int(s.get("PASS") or 0)
                merged_summary["FAIL"] += int(s.get("FAIL") or 0)
                merged_summary["BLOCKED"] += int(s.get("BLOCKED") or 0)
                last_cov = part.get("coverage") or last_cov
                hints = part.get("failureCodeHints") or {}
                if isinstance(hints, dict):
                    for k, v in hints.items():
                        if isinstance(k, str) and isinstance(v, str):
                            merged_hints[k] = v

                retry_stats = part.get("retryStats") or {}
                if isinstance(retry_stats, dict):
                    merged_retry_stats["eligibleRows"] += int(retry_stats.get("eligibleRows") or 0)
                    merged_retry_stats["ineligibleRows"] += int(retry_stats.get("ineligibleRows") or 0)
                    by_class = retry_stats.get("byClass") if isinstance(retry_stats.get("byClass"), dict) else {}
                    for cls, cnt in by_class.items():
                        if isinstance(cls, str):
                            merged_retry_stats["byClass"][cls] = int(merged_retry_stats["byClass"].get(cls, 0)) + int(cnt or 0)

                part_chain = part.get("chainStatuses") if isinstance(part.get("chainStatuses"), dict) else {}
                for k, v in part_chain.items():
                    if isinstance(k, str):
                        merged_chain_statuses[k] = str(v or "")

                part_metrics = part.get("metrics") if isinstance(part.get("metrics"), dict) else {}
                merged_metrics["completed_rows"] += int(part_metrics.get("completed_rows") or len(part.get("rows") or []))
                merged_metrics["target_rows"] = int(part_metrics.get("target_rows") or merged_metrics.get("target_rows") or len(rows_all))
                done = min(len(rows_all), int(merged_metrics.get("completed_rows") or 0))
                elapsed_ms = int(time.time() * 1000) - int(execute_jobs[job_id].get("startedAt") or int(time.time() * 1000))
                eta_ms = None
                if done > 0:
                    avg_per_row = elapsed_ms / done
                    eta_ms = int(max(0, (len(rows_all) - done) * avg_per_row))
                execute_jobs[job_id]["progress"] = {
                    "phase": "execute",
                    "doneRows": done,
                    "totalRows": len(rows_all),
                    "completed_rows": done,
                    "target_rows": len(rows_all),
                    "percent": int((done / max(1, len(rows_all))) * 100),
                    "elapsedMs": elapsed_ms,
                    "etaMs": eta_ms,
                    "lastMessage": f"{done}/{len(rows_all)} 행 처리 완료",
                }

            final_sheet = write_final_testsheet(cfg["run_id"], cfg["project_name"], merged_rows)
            merged_retry_stats["totalRows"] = len(merged_rows)
            merged_retry_stats["retryRate"] = round(int(merged_retry_stats.get("eligibleRows", 0)) / max(1, len(merged_rows)), 3)
            graph_payload = build_execution_graph(merged_rows, merged_chain_statuses)
            final_summary = _build_final_summary(merged_summary, merged_hints)
            execute_jobs[job_id] = {
                **execute_jobs[job_id],
                "ok": True,
                "status": "done",
                "summary": merged_summary,
                "finalSummary": final_summary,
                "coverage": last_cov,
                "metrics": {"completed_rows": len(merged_rows), "target_rows": len(rows_all)},
                "failureCodeHints": merged_hints,
                "retryStats": merged_retry_stats,
                "chainStatuses": merged_chain_statuses,
                "graph": graph_payload,
                "executionGraph": graph_payload,
                "rows": merged_rows,
                "decompositionRows": merged_decomp_rows,
                "finalSheet": final_sheet,
                "progress": {
                    "phase": "done",
                    "doneRows": len(rows_all),
                    "totalRows": len(rows_all),
                    "completed_rows": len(rows_all),
                    "target_rows": len(rows_all),
                    "percent": 100,
                    "elapsedMs": int(time.time() * 1000) - int(execute_jobs[job_id].get("startedAt") or int(time.time() * 1000)),
                    "etaMs": 0,
                    "lastMessage": "실행 완료",
                },
                "endedAt": int(time.time() * 1000),
            }
        except Exception as e:
            execute_jobs[job_id] = {
                **execute_jobs[job_id],
                "ok": False,
                "status": "error",
                "error": str(e),
                "errorDetail": _error_detail("server", "EXECUTE_JOB_FAILED", "비동기 실행 중 오류가 발생했습니다.", str(e)),
                "progress": {
                    **(execute_jobs[job_id].get("progress") or {}),
                    "phase": "error",
                    "lastMessage": "실행 실패",
                },
                "endedAt": int(time.time() * 1000),
            }

    asyncio.create_task(_runner())
    return {"ok": True, "jobId": job_id, "status": "queued", "progress": execute_jobs[job_id].get("progress")}


@app.get("/api/checklist/execute/status/{job_id}")
async def checklist_execute_status(job_id: str) -> Dict[str, Any]:
    job = execute_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "job not found"})
    return job


@app.delete("/api/checklist/execute/status/{job_id}")
async def checklist_execute_status_delete(job_id: str) -> Dict[str, Any]:
    cleanup = _cleanup_entities([], [job_id])
    return {"ok": True, "jobId": job_id, "deleted": job_id in set(cleanup.get("jobIds") or [])}


@app.post("/api/cleanup/chain")
async def cleanup_chain(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    analysis_ids = payload.get("analysisIds") if isinstance(payload.get("analysisIds"), list) else []
    job_ids = payload.get("jobIds") if isinstance(payload.get("jobIds"), list) else []
    artifact_paths = payload.get("artifactPaths") if isinstance(payload.get("artifactPaths"), list) else []

    cleaned = _cleanup_entities(analysis_ids, job_ids, artifact_paths=artifact_paths)
    requested_analysis = [str(x or "").strip() for x in analysis_ids if str(x or "").strip()]
    requested_jobs = [str(x or "").strip() for x in job_ids if str(x or "").strip()]
    requested_artifacts = [str(x or "").strip() for x in artifact_paths if str(x or "").strip()]

    residual = {
        "analysisIds": [x for x in requested_analysis if x not in set(cleaned.get("analysisIds") or [])],
        "jobIds": [x for x in requested_jobs if x not in set(cleaned.get("jobIds") or [])],
        "artifactPaths": [x for x in requested_artifacts if x not in set(cleaned.get("artifacts") or [])],
    }

    return {
        "ok": True,
        "deleted": cleaned,
        "residual": residual,
    }


@app.post("/api/checklist/execute/graph")
async def checklist_execute_graph(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    chain_statuses = payload.get("chainStatuses") if isinstance(payload.get("chainStatuses"), dict) else {}
    graph_payload = build_execution_graph(rows, chain_statuses)
    return {
        "ok": True,
        "graph": graph_payload,
        "executionGraph": graph_payload,
    }


@app.get("/api/qa/templates")
async def qa_templates() -> Dict[str, Any]:
    return {"ok": True, "templates": list_templates()}


@app.post("/api/flow/transition-check")
async def flow_transition_check(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    steps = payload.get("steps") or []
    template_key = str(payload.get("templateKey") or "").strip()
    base_url = str(payload.get("baseUrl") or "").strip()
    if template_key:
        steps = build_template_steps(template_key, base_url)

    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "steps required (or invalid templateKey/baseUrl)"})
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}

    out = await run_transition_check(steps, auth=auth)
    if not out.get("ok"):
        raise HTTPException(status_code=500, detail={"ok": False, "error": out.get("error")})
    return out


@app.post("/api/report/finalize")
async def report_finalize(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    run_id = str(payload.get("runId", "")).strip() or f"run_{int(time.time()*1000)}"
    project_name = str(payload.get("projectName", "QA 테스트시트")).strip()
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "items required"})

    paths = write_final_testsheet(run_id, project_name, items)
    return {"ok": True, "runId": run_id, "projectName": project_name, "finalSheet": paths}


@app.post("/api/checklist/auto")
async def checklist_auto(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    analysis_id = str(payload.get("analysisId", "")).strip()
    if not analysis_id:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "analysisId required"})

    bundle = _load_bundle(analysis_id)
    if not bundle:
        raise HTTPException(status_code=404, detail={"ok": False, "error": "analysis not found"})

    provider, model, _ = _resolve_llm(payload)
    include_auth = bool(payload.get("includeAuth", True))
    max_pages_raw = payload.get("maxPages", None)
    max_pages = int(max_pages_raw) if str(max_pages_raw or "").strip() else None
    source = str(payload.get("source", "sitemap")).strip().lower() or "sitemap"
    auth_payload = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    auth_bundle = bundle.get("auth") if isinstance(bundle.get("auth"), dict) else {}
    auth = {**auth_bundle, **auth_payload}
    checklist_expand = bool(payload.get("checklistExpand", False))
    checklist_expand_mode = str(payload.get("checklistExpandMode", "none") or "none").strip()
    checklist_expand_limit = int(payload.get("checklistExpandLimit", 20) or 20)

    out = await auto_checklist_from_sitemap(
        bundle,
        provider=provider,
        model=model,
        include_auth=include_auth,
        max_pages=max_pages,
        source=source,
        auth=auth,
        checklist_expand=checklist_expand,
        checklist_expand_mode=checklist_expand_mode,
        checklist_expand_limit=checklist_expand_limit,
    )
    try:
        run_id = f"auto_{analysis_id}_{int(time.time())}"
        out["finalSheet"] = write_final_testsheet(
            run_id,
            str(payload.get("projectName") or "QA 테스트시트"),
            out.get("rows") or [],
        )
    except Exception:
        pass
    return out


async def _run_oneclick_single(base_url: str, provider: Any = None, model: str | None = None, auth: Dict[str, Any] | None = None, llm_auth: Dict[str, Any] | None = None) -> Dict[str, Any]:
    analyzed = await analyze_site(base_url, provider=provider, model=model, llm_auth=llm_auth)
    analysis_id = str(analyzed.get("analysisId") or f"py_analysis_{int(time.time() * 1000)}")
    native_pages = analyzed.get("_native", {}).get("pages") or [analyzed.get("_native", {}).get("page", {})]
    _save_native_bundle(
        analysis_id,
        base_url,
        native_pages,
        [],
        analyzed.get("candidates", []),
        reports=analyzed.get("reports", {}),
        auth=(auth or {}),
    )

    candidates = analyzed.get("candidates", [])
    auto_flows = []
    for c in candidates[:3]:
        auto_flows.append(
            {
                "name": str(c.get("name") or "Auto Flow"),
                "loginMode": "OPTIONAL" if analyzed.get("authLikely") else "OFF",
                "steps": [
                    {"action": "NAVIGATE", "targetUrl": "/"},
                    {"action": "ASSERT_URL", "targetUrl": urlparse(base_url).hostname or "/"},
                ],
            }
        )

    if not auto_flows:
        auto_flows = [
            {
                "name": "Smoke",
                "loginMode": "OFF",
                "steps": [
                    {"action": "NAVIGATE", "targetUrl": "/"},
                    {"action": "ASSERT_URL", "targetUrl": urlparse(base_url).hostname or "/"},
                ],
            }
        ]

    finalized = finalize_flows(native_analysis_store, analysis_id, auto_flows)
    if not finalized.get("ok"):
        return {"ok": False, "error": finalized.get("error") or "finalize failed", "status": 500}
    save_flows(analysis_id, auto_flows)

    ran = await run_flows(native_analysis_store, analysis_id, provider=provider, model=model, llm_auth=llm_auth)
    if not ran.get("ok"):
        return {"ok": False, "error": ran.get("error"), "status": int(ran.get("status") or 500)}

    summary = ran.get("summary") or {}
    failure_hints = ran.get("failureCodeHints") or {}
    return {
        "ok": True,
        "analysisId": analysis_id,
        "runId": ran.get("runId"),
        "finalStatus": ran.get("finalStatus"),
        "summary": summary,
        "finalSummary": _build_final_summary(summary, failure_hints),
        "judge": ran.get("judge"),
        "failureCodeHints": failure_hints,
        "reportPath": ran.get("reportPath", ""),
        "reportJson": ran.get("reportJson", ""),
        "fixSheet": ran.get("fixSheet"),
        "discovered": {
            "pages": analyzed.get("pages"),
            "elements": analyzed.get("elements"),
            "serviceType": analyzed.get("serviceType"),
            "authLikely": analyzed.get("authLikely"),
            "metrics": analyzed.get("metrics"),
        },
        "plannerMode": analyzed.get("plannerMode"),
        "plannerReason": analyzed.get("plannerReason"),
        "analysisReports": analyzed.get("reports", {}),
    }


@app.post("/api/oneclick")
async def oneclick(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    provider, model, llm_auth = _resolve_llm(payload)

    dual = payload.get("dualContext") if isinstance(payload.get("dualContext"), dict) else {}
    if dual:
        user_base = str(dual.get("userBaseUrl") or payload.get("baseUrl") or "").strip()
        admin_base = str(dual.get("adminBaseUrl") or user_base).strip()
        admin_auth = dual.get("adminAuth") if isinstance(dual.get("adminAuth"), dict) else (payload.get("auth") if isinstance(payload.get("auth"), dict) else {})
        auto_user_signup = bool(dual.get("autoUserSignup", True))
        if not user_base:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "dualContext.userBaseUrl required"})

        user_res = await _run_oneclick_single(user_base, provider=provider, model=model, auth={}, llm_auth=llm_auth)
        if not user_res.get("ok"):
            raise HTTPException(status_code=int(user_res.get("status") or 500), detail={"ok": False, "error": f"user flow failed: {user_res.get('error')}"})

        signup_result: Dict[str, Any] = {
            "status": "SKIPPED",
            "reason": "autoUserSignup disabled",
            "signals": {"autoUserSignup": False},
        }
        if auto_user_signup:
            user_bundle = _load_bundle(str(user_res.get("analysisId") or "")) or {}
            try:
                signup_result = await attempt_user_signup(user_base, user_bundle)
            except Exception as e:
                signup_result = {"status": "FAILED", "reason": str(e), "signals": {"autoUserSignup": True}}

        admin_res = await _run_oneclick_single(admin_base, provider=provider, model=model, auth=admin_auth, llm_auth=llm_auth)
        if not admin_res.get("ok"):
            raise HTTPException(status_code=int(admin_res.get("status") or 500), detail={"ok": False, "error": f"admin flow failed: {admin_res.get('error')}"})

        user_phase = [
            {"name": "user.analyze+run", "status": "PASS" if user_res.get("ok") else "FAIL", "analysisId": user_res.get("analysisId"), "runId": user_res.get("runId")},
            {"name": "user.signupAttempt", **signup_result},
        ]
        admin_phase = [
            {"name": "admin.analyze+run", "status": "PASS" if admin_res.get("ok") else "FAIL", "analysisId": admin_res.get("analysisId"), "runId": admin_res.get("runId")},
        ]
        bridge_phase = [
            {
                "name": "bridge.user-admin-consistency",
                "status": "PASS" if (user_res.get("finalStatus") == "PASS" and admin_res.get("finalStatus") == "PASS") else "WARN",
                "note": "admin 처리 후 user 상태 반영은 transition-check/template에서 검증 권장",
            }
        ]

        return {
            "ok": True,
            "oneClick": True,
            "oneClickDual": True,
            "dualContext": {
                "userBaseUrl": user_base,
                "adminBaseUrl": admin_base,
                "autoUserSignup": auto_user_signup,
            },
            "user": user_res,
            "admin": admin_res,
            "userPhase": user_phase,
            "adminPhase": admin_phase,
            "bridgePhase": bridge_phase,
            "finalStatus": "PASS" if (user_res.get("finalStatus") == "PASS" and admin_res.get("finalStatus") == "PASS") else "PASS_WITH_WARNINGS",
            "summary": {"user": user_res.get("summary"), "admin": admin_res.get("summary")},
            "analysisReports": {"user": user_res.get("analysisReports", {}), "admin": admin_res.get("analysisReports", {})},
        }

    base_url = str(payload.get("baseUrl", "")).strip()
    if not base_url:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "baseUrl required"})
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    single = await _run_oneclick_single(base_url, provider=provider, model=model, auth=auth, llm_auth=llm_auth)
    if not single.get("ok"):
        raise HTTPException(status_code=int(single.get("status") or 500), detail={"ok": False, "error": single.get("error")})
    return {"ok": True, "oneClick": True, **single}


@app.post("/api/flows/finalize")
async def flows_finalize(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    analysis_id = str(payload.get("analysisId", "")).strip()
    flows = payload.get("flows") or []
    if not analysis_id:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "analysisId required"})
    if not isinstance(flows, list) or len(flows) == 0:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "flows required"})

    _load_bundle(analysis_id)
    r = finalize_flows(native_analysis_store, analysis_id, flows)
    if not r.get("ok"):
        code = int(r.get("status") or 400)
        raise HTTPException(status_code=code, detail={"ok": False, "error": r.get("error")})
    save_flows(analysis_id, flows)
    return r


@app.post("/api/flows/run")
async def flows_run(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    analysis_id = str(payload.get("analysisId", "")).strip()
    if not analysis_id:
        raise HTTPException(status_code=400, detail={"ok": False, "error": "analysisId required"})

    provider, model, llm_auth = _resolve_llm(payload)
    _load_bundle(analysis_id)
    r = await run_flows(native_analysis_store, analysis_id, provider=provider, model=model, llm_auth=llm_auth)
    if not r.get("ok"):
        code = int(r.get("status") or 400)
        raise HTTPException(status_code=code, detail={"ok": False, "error": r.get("error")})
    return r
