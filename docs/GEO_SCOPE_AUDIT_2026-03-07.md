# GEO Scope Audit (2026-03-07)

목표: `GEO 테스트 프로그램`만 남기기 위해 QA 혼합 기능 제거 범위를 확정한다.
상태: 2026-03-07 기준 1차 삭제/리팩토링 적용 완료 (analyze/llm 보류 유지)

## 1) 유지(Keep) 대상

- FastAPI 앱 엔트리
  - `app/main.py` (단, GEO/공통 라우팅/미들웨어만)
- GEO 라우터
  - `app/routers/geo.py`
  - 엔드포인트: `GET /geo-test`, `POST /api/geo-audit`
- GEO 디스커버리 라우터
  - `app/routers/discovery.py`
  - 엔드포인트: `POST /api/geo-discovery`
- GEO 분석 로직
  - `app/services/geo_audit.py`
- GEO 고도화 재사용(보류 유지)
  - `app/services/analyze.py`
  - `app/services/llm.py` (`analyze.py`의 LLM fallback 경로 의존)
- GEO 테스트 페이지
  - `app/static/geo-test.html`
- GEO 테스트
  - `tests/test_geo_test_page.py`
  - `tests/test_geo_audit_details.py`

## 2) 삭제 후보 (기능 단위)

아래는 GEO 전용 운영 관점에서 제거해도 되는 QA 기능들이다.

### A. QA API 엔드포인트 (app/main.py)

- `/api/sheets/pull` (POST/GET)
- `/api/llm/oauth/start`, `/api/llm/oauth/callback`, `/api/llm/oauth/status`, `/api/llm/oauth/logout`
- `/api/analyze`, `/api/analysis/{analysis_id}` (GET/DELETE)
- `/api/flow-map`, `/api/structure-map`, `/api/condition-matrix`
- `/api/checklist`
- `/api/checklist/execute`, `/api/checklist/execute/async`
- `/api/checklist/execute/status/{job_id}` (GET/DELETE)
- `/api/cleanup/chain`
- `/api/checklist/execute/graph`
- `/api/qa/templates`
- `/api/flow/transition-check`
- `/api/report/finalize`
- `/api/checklist/auto`
- `/api/oneclick`
- `/api/flows/finalize`, `/api/flows/run`

### B. QA 서비스 모듈 (app/services)

- `checklist.py`
- `condition_matrix.py`
- `entity_map.py`
- `execute_checklist.py`
- `final_output.py`
- `flow_map.py`
- `flows.py`
- `google_sheets.py`
- `page_audit.py`
- `qa_templates.py`
- `reporting.py`
- `site_profile.py`
- `state_transition.py`
- `storage.py`
- `structure_map.py`
- `user_signup.py`

### C. QA 테스트 코드 (tests)

- `test_cleanup_and_route_role.py`
- `test_density_and_finalize.py`
- `test_fix_sheet_autoroute.py`
- `test_interaction_linking.py`

### D. QA 문서/운영 스크립트

- `docs/API_SPEC.md`
- `docs/HANDOFF.md`
- `docs/OAUTH_SETUP.md`
- `docs/OPS_AUTOMATION.md`
- `docs/OPS_STABILITY_EVIDENCE_2026-02-20.md`
- `docs/QA_GUARD_BE.md`
- `docs/QA_HARDENING_EXEC_PLAN.md`
- `docs/SHEET_ATOMICITY_RULES.md`
- `scripts/ci_guard.sh`
- `scripts/guarded_push_main.sh`
- `scripts/ops_split_check.sh`
- `scripts/setup_ci_guard_hooks.sh`
- `scripts/smoke_candidate_parity.py`
- `.githooks/*` (QA guard 용도면 함께 제거 후보)

### E. 중복 서브프로젝트

- `geo-audit-be/` 전체
  - 현재 루트 `app/`에도 GEO 구현이 존재해 중복 유지 시 혼란 발생.
  - 둘 중 하나만 남기는 것이 좋다. (권장: 루트 `app/` 유지, `geo-audit-be/` 제거)

### F. 의존성 정리 후보

- `requirements.txt`에서 GEO 무관 패키지 제거 후보:
  - `XlsxWriter`
  - `google-api-python-client`
  - `google-auth`

## 2.1) 적용 결과 (완료)

- QA API/서비스/테스트/문서/스크립트 삭제 반영
- 중복 `geo-audit-be/` 코드 파일 삭제 반영
- `requirements.txt`에서 GEO 무관 패키지 제거 반영
- `app/services/analyze.py`, `app/services/llm.py`는 GEO 고도화 용도로 유지
- `POST /api/geo-discovery` 추가로 analyze/llm 재사용 경로 연결

## 2.2) GEO 고도화 (optiflow.kr/geo 기준 반영)

- `app/services/geo_audit.py` 개선 적용:
  - 페이지 단위 `JSON-LD` 커버리지/유효성 집계(`json_ld_summary`, `json_ld_pages`)
  - `robots.txt`의 `Sitemap:` 라인 해석 기반 sitemap 탐지 보강
  - `llms.txt` 정성 품질 점수(`llms_txt_quality`) 추가
  - 머신 가독 신호(`machine_readable`) 추가:
    - Next.js `__NEXT_DATA__` 파싱
    - `article:*`, `author`, `h:*` 메타 시그널 탐지
  - 한국어 FAQ/엔터티 탐지 패턴 보강

- 결과:
  - `https://optiflow.kr/geo` 기준 GEO audit 점수 92점(로컬 검증)
  - JSON-LD 10/10 유효, 추천사항 없음

## 3) 삭제 전 최종 확인 체크

- `app/main.py`에서 QA 라우트 제거 후에도 `GET /`, `GET /health`, `GET /geo-test`, `POST /api/geo-audit` 정상 동작
- `tests/test_geo_test_page.py`, `tests/test_geo_audit_details.py` 통과
- README/API 문서가 GEO 기준으로 갱신됨
- `requirements.txt` 최소 의존성으로 재잠금/재검증
