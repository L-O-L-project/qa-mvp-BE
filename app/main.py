from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.routers.discovery import router as discovery_router
from app.routers.geo import router as geo_router

APP_NAME = "geo-mvp-fastapi"
WEB_ORIGIN = os.getenv("QA_WEB_ORIGIN", "*").strip() or "*"


app = FastAPI(title=APP_NAME, version="0.2.0")
app.include_router(geo_router)
app.include_router(discovery_router)


def _error_detail(category: str, code: str, user_message: str, debug_detail: Any = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "errorCategory": category,
        "errorCode": code,
        "userMessage": user_message,
        "debugDetail": debug_detail,
    }


@app.middleware("http")
async def _catch_unhandled_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=_error_detail("server", "UNHANDLED_EXCEPTION", "서버 내부 오류가 발생했습니다.", str(e)),
        ) from e


allow_origins = ["*"] if WEB_ORIGIN == "*" else [WEB_ORIGIN]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": APP_NAME,
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": APP_NAME,
    }
