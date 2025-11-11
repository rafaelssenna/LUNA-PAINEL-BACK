# app/routes/media.py
from __future__ import annotations

import io
import re
import json
import base64
from typing import Dict, Any, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from starlette.responses import StreamingResponse

from app.routes.deps import get_uazapi_ctx
from app.routes.ai import classify_by_rules

# services (assíncronos!)
from app.services.lead_status import (
    getCachedLeadStatus,
    upsertLeadStatus,
    needsReclassify,
)

# <<< sem prefixo aqui; prefix é aplicado no main.py >>>
router = APIRouter()

# ---------------- utils ----------------
def _uaz(ctx: Dict[str, Any]):
    base = f"https://{ctx['host']}"
    headers = {"token": ctx["token"]}
    return base, headers

def _pick(d: Dict[str, Any], path: str, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default

def _b64url_to_bytes(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)

def _get_instance_id_from_request(req: Request) -> str:
    inst = getattr(req.state, "instance_id", None)
    if inst:
        return str(inst)
    h = req.headers.get("x-instance-id")
    if h:
        return str(h)
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        parts = token.split(".")
        if len(parts) >= 2:
            try:
                payload = json.loads(_b64url_to_bytes(parts[1]).decode("utf-8"))
                return str(
                    payload.get("instance_id")
                    or payload.get("phone_number_id")
                    or payload.get("pnid")
                    or payload.get("sub")
                    or ""
                )
            except Exception:
                pass
    return ""

# ---------------- media proxy/resolve ----------------
@router.get("/proxy")
async def media_proxy(u: str = Query(..., min_length=4)):
    if not re.match(r"^https?://", u):
        raise HTTPException(400, "URL inválida")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
            r = await cli.get(u)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, "Falha ao baixar mídia")
        ct = r.headers.get("content-type", "application/octet-stream")
        return StreamingResponse(io.BytesIO(r.content), media_type=ct)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"proxy erro: {e}")

@router.post("/resolve")
async def media_resolve(payload: Dict[str, Any] = Body(...), ctx=Depends(get_uazapi_ctx)):
    if not ctx or "host" not in ctx or "token" not in ctx:
        raise HTTPException(500, "Contexto UAZ inválido")

    m = payload or {}

    mime = (
        m.get("mimetype") or m.get("mime")
        or _pick(m, "message.imageMessage.mimetype")
        or _pick(m, "message.videoMessage.mimetype")
        or _pick(m, "message.documentMessage.mimetype")
        or _pick(m, "message.audioMessage.mimetype")
        or _pick(m, "message.stickerMessage.mimetype")
        or ""
    )
    url = (
        m.get("mediaUrl") or m.get("url") or m.get("fileUrl") or m.get("downloadUrl")
        or m.get("image") or m.get("video")
        or _pick(m, "message.imageMessage.url")
        or _pick(m, "message.videoMessage.url")
        or _pick(m, "message.documentMessage.url")
        or _pick(m, "message.audioMessage.url")
        or _pick(m, "message.stickerMessage.url")
        or ""
    )
    data_url = (
        m.get("dataUrl")
        or _pick(m, "message.imageMessage.dataUrl")
        or _pick(m, "message.videoMessage.dataUrl")
        or _pick(m, "message.stickerMessage.dataUrl")
        or ""
    )
    if url or data_url:
        return {"url": url, "mime": mime, "dataUrl": data_url}

    media_id = (
        m.get("mediaId")
        or _pick(m, "message.documentMessage.mediaKey")
        or _pick(m, "message.imageMessage.mediaKey")
        or _pick(m, "message.videoMessage.mediaKey")
        or _pick(m, "message.audioMessage.mediaKey")
        or _pick(m, "message.stickerMessage.mediaKey")
        or None
    )

    base, headers = _uaz(ctx)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
        candidates = []
        if media_id:
            candidates.append(("GET", f"{base}/media/resolve?id={media_id}", None))
        candidates.append(("POST", f"{base}/media/resolve", {"message": m}))
        for method, url2, body in candidates:
            r = await (cli.post(url2, headers=headers, json=body) if method == "POST" else cli.get(url2, headers=headers))
            if 200 <= r.status_code < 300:
                try:
                    j = r.json()
                except Exception:
                    continue
                u2 = j.get("url") or j.get("downloadUrl")
                mm = j.get("mime") or j.get("mimetype") or mime
                d2 = j.get("dataUrl") or ""
                if u2 or d2:
                    return {"url": u2, "mime": mm, "dataUrl": d2}

    raise HTTPException(404, "Não foi possível resolver a mídia")

# ---------------- Classificação com cache ----------------
def _ts(m: Dict[str, Any]) -> int:
    return int(
        m.get("messageTimestamp")
        or m.get("timestamp")
        or m.get("t")
        or (m.get("message") or {}).get("messageTimestamp")
        or 0
    )

def _from_me(m: Dict[str, Any]) -> bool:
    return bool(
        m.get("fromMe")
        or m.get("fromme")
        or m.get("from_me")
        or (m.get("key") or {}).get("fromMe")
        or (m.get("message") or {}).get("key", {}).get("fromMe")
    )

@router.post("/stage/classify")
async def stage_classify(request: Request, payload: Dict[str, Any] = Body(...)):
    """
    payload: { chatid?: str, messages: [...] }
    - Lê instance_id do JWT
    - Usa cache (DB) se não precisar reclassificar
    - Caso precise, classifica, persiste e retorna
    """
    instance_id = _get_instance_id_from_request(request)
    if not instance_id:
        raise HTTPException(401, "JWT sem instance_id")

    chatid = str(payload.get("chatid") or "").strip()
    items: List[Dict[str, Any]] = payload.get("messages") or []

    last = max(items, key=_ts) if items else None
    last_ts = _ts(last) if last else 0
    last_from_me = _from_me(last) if last else False

    # cache: se não precisa reclassificar, devolve do banco
    if chatid and not await needsReclassify(instance_id, chatid, last_ts, last_from_me):
        cached = await getCachedLeadStatus(instance_id, chatid)
        if cached:
            return {
                "stage": cached["stage"],
                "cached": True,
                "last_msg_ts": int(cached.get("last_msg_ts") or 0),
            }

    # classifica pelas regras atuais
    stage = classify_by_rules(items) or "contatos"

    # persiste no cache por instância
    if chatid:
        rec = await upsertLeadStatus(
            instance_id=instance_id,
            chatid=chatid,
            stage=stage,
            last_msg_ts=last_ts,
            last_from_me=last_from_me,
        )
        return {
            "stage": rec["stage"],
            "cached": False,
            "last_msg_ts": int(rec.get("last_msg_ts") or last_ts),
        }

    return {"stage": stage, "cached": False, "last_msg_ts": last_ts}
