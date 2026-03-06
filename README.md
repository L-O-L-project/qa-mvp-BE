# qa-mvp-BE

Backend repo for QA MVP (FastAPI).

## Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Main endpoints
- `/geo-test` (web test page)
- `/api/geo-audit`
- `/api/analyze`
- `/api/checklist/auto`
- `/api/checklist/execute`
- `/api/checklist/execute/async` + `/api/checklist/execute/status/{jobId}`
- `/api/flow/transition-check`
- `/api/report/finalize`
- `/api/oneclick`
- `/api/sheets/pull` (Phase-1 read-only prototype)

## QA Hardening API additions (P1)
- Progress schema 강화 (`progress.phase`, `percent`, `elapsedMs`, `etaMs`, `lastMessage`)
- Final summary 제공 (`finalSummary.critical_fail_count`, `warning_count`, `blockers_top`, `action_items`, `decision_hint`)
- Error taxonomy 표준화 (`errorCategory`, `errorCode`, `userMessage`, `debugDetail`)

## Google Sheets Phase-1 (pull-only)
Environment variables:
- `QA_SHEETS_SPREADSHEET_ID` (required)
- `QA_SHEETS_AUTH_MODE` (`service_account` or `oauth`)
- `QA_SHEETS_SERVICE_ACCOUNT_JSON` (required for `service_account`, file path)
- `QA_SHEETS_OAUTH_ACCESS_TOKEN` (required for `oauth`, placeholder mode)

Notes:
- Endpoint only reads from `checklist`, `execution`, `fix_sheet`
- Validation errors are returned in response and logged
- Audit log is written to `out/google_sheets_audit.jsonl`
- Writes are intentionally disabled in Phase-1

See `docs/API_SPEC.md`.

OAuth setup: `docs/OAUTH_SETUP.md`

## Backend full smoke
```bash
FASTAPI_BASE=http://127.0.0.1:8000 bash ./scripts/ops_split_check.sh
```
