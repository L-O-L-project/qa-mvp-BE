# geo-mvp-BE

FastAPI backend for GEO audit testing.

## Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Test
```bash
python3 -m pytest -q
```

## Endpoints
- `GET /` service info
- `GET /health` health check
- `GET /geo-test` GEO web test page
- `POST /api/geo-audit` run GEO audit
- `POST /api/geo-discovery` run site discovery for GEO enhancement

`/api/geo-audit` includes:
- `json_ld_summary` page-level JSON-LD coverage
- `llms_txt_quality` llms.txt quality scoring
- `machine_readable` signals (`__NEXT_DATA__`, article meta, `h:*` meta)

## GEO enhancement reserve
The following modules are intentionally kept for future GEO enhancement:
- `app/services/analyze.py`
- `app/services/llm.py`
