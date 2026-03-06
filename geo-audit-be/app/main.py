from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.analysis.pipeline import run_geo_audit


class GeoAuditRequest(BaseModel):
    url: str


app = FastAPI(title="geo-audit-be", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def test_page() -> FileResponse:
    return FileResponse(Path(__file__).with_name("test_page.html"))


@app.post("/geo-audit")
async def geo_audit(payload: GeoAuditRequest):
    try:
        return await run_geo_audit(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
