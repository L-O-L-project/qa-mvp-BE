---
title: GEO Audit Demo
emoji: 🌐
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
base_path: /geo-test
fullWidth: true
header: mini
short_description: Public GEO audit test page backed by FastAPI APIs.
pinned: false
---

# geo-mvp-BE

GEO 전용 FastAPI 백엔드입니다.

## 핵심 엔드포인트
- `GET /` 서비스 정보
- `GET /health` 헬스체크
- `GET /geo-test` GEO 테스트 페이지
- `POST /api/geo-audit` GEO 진단 실행
- `POST /api/geo-discovery` 사이트 디스커버리 실행

## 실행
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## 테스트
```bash
python3 -m pytest -q
```

## API 예시
```bash
curl -sS -X POST http://127.0.0.1:8000/api/geo-audit \
  -H "Content-Type: application/json" \
  -d '{"url":"https://optiflow.kr/geo"}'
```

```bash
curl -sS -X POST http://127.0.0.1:8000/api/geo-discovery \
  -H "Content-Type: application/json" \
  -d '{"baseUrl":"https://optiflow.kr"}'
```

## GEO Audit 주요 결과 필드
- `geo_score`
- `checks`
- `recommendations`
- `evidence.json_ld_summary` (페이지별 JSON-LD 커버리지)
- `evidence.llms_txt_quality` (llms.txt 품질 점수)
- `evidence.machine_readable` (`__NEXT_DATA__`, article/h:* meta 신호)

## 환경 변수
- `QA_WEB_ORIGIN`: CORS 허용 origin (`*` 기본)
- `QA_HTTP_VERIFY_TLS`: 크롤링 시 TLS 검증 여부 (`false` 기본)
- `QA_GEO_DYNAMIC`: Playwright 동적 링크 수집 사용 여부 (`false` 기본)
- `QA_ANALYZE_DYNAMIC`: discovery 동적 링크 수집 사용 여부 (`true` 기본)

## 무료 배포
- 공개 테스트 페이지 + API 데모는 Hugging Face Docker Spaces가 가장 간단합니다.
- 배포 파일은 `Dockerfile`, `.dockerignore`가 포함되어 있습니다.
- 절차는 [Hugging Face Spaces Deploy](docs/HUGGINGFACE_SPACES_DEPLOY.md) 참고

## 참고 문서
- [GEO Scope Audit](docs/GEO_SCOPE_AUDIT_2026-03-07.md)
- [GEO Handoff](docs/GEO_HANDOFF.md)

## GEO 고도화용 유지 모듈
- `app/services/analyze.py`
- `app/services/llm.py`
