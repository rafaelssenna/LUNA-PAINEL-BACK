# app/routes/lead_status.py
from __future__ import annotations
from typing import Dict, Any, List
from fastapi import APIRouter, Query, Body, HTTPException, Request
import logging
import base64
import json

# tenta importar a versão bulk do service (assíncrona)
try:
    from app.services.lead_status import (  # type: ignore
        getCachedLeadStatus,
        getCachedLeadStatusBulk,
    )
    _HAS_BULK = True
except Exception:
    # fallback assíncrono chamando o unitário em loop (com await!)
    from app.services.lead_status import getCachedLeadStatus  # type: ignore
    _HAS_BULK = False

    async def getCachedLeadStatusBulk(instance_id: str, chatids: List[str]):
        out = []
        for cid in chatids:
            rec = await getCachedLeadStatus(instance_id, cid)
            if rec:
                out.append(rec)
        return out


router = APIRouter()
log = logging.getLogger("uvicorn.error")


# ---------------- util: extrai instance_id do JWT sem dependências externas
def _b64url_to_bytes(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _get_instance_id_from_request(req: Request) -> str:
    # 1) se algum middleware já setou (opcional)
    inst = getattr(req.state, "instance_id", None)
    if inst:
        return str(inst)

    # 2) header extra opcional
    h = req.headers.get("x-instance-id")
    if h:
        return str(h)

    # 3) decodifica o JWT (sem verificar) e pega claim
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        parts = token.split(".")
        if len(parts) >= 2:
            try:
                payload = json.loads(_b64url_to_bytes(parts[1]).decode("utf-8"))
                val = (
                    payload.get("instance_id")
                    or payload.get("phone_number_id")
                    or payload.get("pnid")
                    or payload.get("sub")
                    or ""
                )
                if val:
                    return str(val)
            except Exception:
                pass

    # 4) fallback controlado para não quebrar chamadas do front
    log.warning("lead-status: request sem instance_id; usando fallback 'default'.")
    return "default"


# ---------------- endpoints
@router.get("/lead-status")
async def get_one(request: Request, chatid: str = Query(..., min_length=1)):
    instance_id = _get_instance_id_from_request(request)

    rec = await getCachedLeadStatus(instance_id, chatid)
    if not rec:
        return {"found": False}

    # sempre retorna escopado por instance_id
    return {"found": True, **rec}


@router.post("/lead-status/bulk")
async def get_bulk(request: Request, payload: Dict[str, Any] = Body(...)):
    instance_id = _get_instance_id_from_request(request)

    # aceita "chatids" ou "ids" por compatibilidade
    raw_ids = payload.get("chatids") or payload.get("ids") or []
    if not isinstance(raw_ids, list) or not all(isinstance(c, str) and c.strip() for c in raw_ids):
        raise HTTPException(400, "chatids inválido")

    # saneia/limita para evitar abuso
    dedup = list(dict.fromkeys([c.strip() for c in raw_ids if c.strip()]))
    if len(dedup) > 2000:
        dedup = dedup[:2000]

    # log pra confirmar chegada do bulk
    log.info("lead-status/bulk req: instance=%s, qtd=%d", instance_id, len(dedup))

    items = await getCachedLeadStatusBulk(instance_id, dedup)
    return {
        "items": items,
        "count": len(items),
        "requested": len(dedup),
        "bulk_accelerated": _HAS_BULK,  # apenas informativo
    }
