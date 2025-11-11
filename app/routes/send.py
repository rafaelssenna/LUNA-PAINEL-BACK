# app/routes/send.py
from __future__ import annotations

import time
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routes.deps import get_uazapi_ctx
from app.routes.deps_billing import require_active_tenant   # ✅ verifica trial/pagamento
from app.services.lead_status import upsert_lead_status     # ✅ snake_case + await

router = APIRouter()

class SendText(BaseModel):
    number: str
    text: str

class SendMedia(BaseModel):
    number: str
    url: str
    caption: str | None = None

class SendButtons(BaseModel):
    number: str
    text: str
    buttons: list[str]

class SendList(BaseModel):
    number: str
    header: str
    body: str
    button_text: str
    sections: list[dict]

def base(host: str) -> str:
    return f"https://{host}"

def hdr(tok: str) -> dict:
    return {"token": tok, "Content-Type": "application/json"}

def to_dict(m: BaseModel) -> dict:
    return m.model_dump() if hasattr(m, "model_dump") else m.dict()

@router.post("/send-text")
async def send_text(
    body: SendText,
    user=Depends(require_active_tenant),   # ✅ bloqueia se expirado
    ctx=Depends(get_uazapi_ctx),
):
    host, tok = ctx["host"], ctx["token"]
    url = f"{base(host)}/send/text"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, headers=hdr(tok), json=to_dict(body))
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        resp = r.json()
        try:
            inst = user.get("instance_id") or ""
            await upsert_lead_status(inst, body.number, None, int(time.time() * 1000), True)  # ✅ ms
        except Exception:
            pass
        return resp

@router.post("/send-media")
async def send_media(
    body: SendMedia,
    user=Depends(require_active_tenant),
    ctx=Depends(get_uazapi_ctx),
):
    host, tok = ctx["host"], ctx["token"]
    url = f"{base(host)}/send/media"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, headers=hdr(tok), json=to_dict(body))
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        resp = r.json()
        try:
            inst = user.get("instance_id") or ""
            await upsert_lead_status(inst, body.number, None, int(time.time() * 1000), True)
        except Exception:
            pass
        return resp

@router.post("/send-buttons")
async def send_buttons(
    body: SendButtons,
    user=Depends(require_active_tenant),
    ctx=Depends(get_uazapi_ctx),
):
    host, tok = ctx["host"], ctx["token"]
    url = f"{base(host)}/send/buttons"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, headers=hdr(tok), json=to_dict(body))
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        resp = r.json()
        try:
            inst = user.get("instance_id") or ""
            await upsert_lead_status(inst, body.number, None, int(time.time() * 1000), True)
        except Exception:
            pass
        return resp

@router.post("/send-list")
async def send_list(
    body: SendList,
    user=Depends(require_active_tenant),
    ctx=Depends(get_uazapi_ctx),
):
    host, tok = ctx["host"], ctx["token"]
    url = f"{base(host)}/send/list"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, headers=hdr(tok), json=to_dict(body))
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        resp = r.json()
        try:
            inst = user.get("instance_id") or ""
            await upsert_lead_status(inst, body.number, None, int(time.time() * 1000), True)
        except Exception:
            pass
        return resp
