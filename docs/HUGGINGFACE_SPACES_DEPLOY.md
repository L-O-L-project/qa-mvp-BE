# Hugging Face Spaces Deploy

이 프로젝트는 `Docker Space`로 올리는 것이 가장 간단합니다. 테스트 페이지와 API가 같은 FastAPI 앱 안에 있으므로, Space 하나만 띄우면 `/geo-test`와 `/api/geo-audit`가 같이 동작합니다.

## 1. 준비된 파일

- `Dockerfile`
- `.dockerignore`

기본 설정은 무료 데모 환경을 고려해 아래처럼 잡혀 있습니다.

- `PORT=7860`
- `QA_GEO_DYNAMIC=false`
- `QA_ANALYZE_DYNAMIC=false`

`geo-discovery`는 동작하지만, 무료 티어 안정성을 위해 Playwright 기반 동적 경로 수집은 기본 비활성화됩니다.

## 2. Space 만들기

1. Hugging Face에서 `Create new Space`
2. SDK는 `Docker` 선택
3. 공개 여부는 필요에 따라 `Public` 선택
4. 생성 후 이 저장소 파일을 Space repo에 업로드

## 3. Space README 템플릿

Space repo의 루트 `README.md`는 아래처럼 두는 것이 가장 안전합니다.

```md
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

# GEO Audit Demo

FastAPI GEO audit demo.

- Test page: `/geo-test`
- Health: `/health`
- GEO audit API: `POST /api/geo-audit`
- GEO discovery API: `POST /api/geo-discovery`
```

## 4. 업로드할 파일

최소 필요 파일:

- `Dockerfile`
- `requirements.txt`
- `app/`
- `docs/` (선택)

현재 저장소 기준으로는 루트 전체를 올려도 됩니다. `.dockerignore`가 불필요한 항목을 제외합니다.

## 5. 배포 후 확인

Space URL이 `https://<space-name>.hf.space` 라면 아래를 확인합니다.

- `https://<space-name>.hf.space/health`
- `https://<space-name>.hf.space/geo-test`

API 예시:

```bash
curl -sS -X POST https://<space-name>.hf.space/api/geo-audit \
  -H "Content-Type: application/json" \
  -d '{"url":"https://optiflow.kr/geo"}'
```

## 6. 주의사항

- 무료 하드웨어는 유휴 시 sleep 될 수 있습니다.
- 첫 요청은 콜드스타트로 느릴 수 있습니다.
- 무거운 사이트 크롤링은 응답이 오래 걸릴 수 있습니다.
- Playwright 동적 수집이 꼭 필요하면 Space Settings에서 환경변수 `QA_GEO_DYNAMIC=true`, `QA_ANALYZE_DYNAMIC=true`로 켤 수 있지만, 무료 티어에서는 비추천입니다.
