from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.services.analyze import analyze_site

router = APIRouter()


def _error_detail(category: str, code: str, user_message: str, debug_detail: Any = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "errorCategory": category,
        "errorCode": code,
        "userMessage": user_message,
        "debugDetail": debug_detail,
    }


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
    return provider, model, llm_auth


@router.post("/api/geo-discovery")
async def geo_discovery(req: Request) -> Dict[str, Any]:
    payload = await _json_payload(req)
    base_url = str(payload.get("baseUrl") or payload.get("url") or "").strip()
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("config", "BASE_URL_REQUIRED", "baseUrl (or url) is required"),
        )

    provider, model, llm_auth = _resolve_llm(payload)

    try:
        return await analyze_site(base_url, provider=provider, model=model, llm_auth=llm_auth)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("config", "INVALID_BASE_URL", "invalid baseUrl", str(e)),
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=502,
            detail=_error_detail("dependency", "DISCOVERY_FAILED", "failed to crawl target url", str(e)),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=_error_detail("server", "DISCOVERY_UNHANDLED", "geo discovery failed", str(e)),
        ) from e
