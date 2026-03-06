# geo-audit-be

Minimal GEO audit backend extracted from `qa-mvp-BE` site analysis architecture.

## Pipeline

URL Input → Crawler (HTTP + optional Playwright) → HTML Fetch → DOM Parse → GEO Analysis → Result Aggregation → JSON Report

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

## API

### `GET /`

브라우저에서 실행 가능한 테스트 페이지를 제공합니다.

### `POST /geo-audit`

Request

```json
{
  "url": "https://example.com"
}
```

Response fields:
- `geo_score`
- `checks`
- `structured_data`
- `recommendations`


## Quick functional check

```bash
curl -sS http://127.0.0.1:8010/health
curl -sS -X POST http://127.0.0.1:8010/geo-audit \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```
