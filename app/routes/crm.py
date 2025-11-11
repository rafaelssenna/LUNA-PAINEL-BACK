# app/routes/crm.py
from __future__ import annotations
import os, json, time, re
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Body

from app.auth import get_current_user            # proteção
from app.routes.deps import get_uazapi_ctx       # host/token da UAZAPI

router = APIRouter()

# ===================== Config/Store =====================

# Estágios OFICIAIS (mantidos como você tem hoje)
STAGES: List[str] = [
    "lead",
    "lead_qualificado",
    "lead_quente",
    "prospectivo_cliente",
    "cliente",
]

DATA_DIR = os.getenv("CRM_DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
CRM_STORE = os.path.join(DATA_DIR, "crm.json")

_store: Dict[str, Dict] = {}


def _load_store() -> None:
    """Carrega o dicionário do CRM do disco."""
    global _store
    try:
        if os.path.exists(CRM_STORE):
            with open(CRM_STORE, "r", encoding="utf-8") as f:
                _store = json.load(f)
        else:
            _store = {}
    except Exception:
        _store = {}


def _save_store() -> None:
    """Salva o dicionário do CRM de forma atômica."""
    tmp = CRM_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_store, f, ensure_ascii=False)
    os.replace(tmp, CRM_STORE)


def _now() -> int:
    return int(time.time())


def _normalize_chatid(chatid: Optional[str] = None, number: Optional[str] = None) -> str:
    """
    Aceita:
      - chatid/wa_chatid (já com @s.whatsapp.net)
      - number (55319...) e converte para @s.whatsapp.net
    """
    if chatid and "@s.whatsapp.net" in chatid:
        return chatid.strip()

    raw = (chatid or number or "").strip()
    if not raw:
        return ""

    if re.fullmatch(r"\d{10,15}", raw):
        return f"{raw}@s.whatsapp.net"

    if "@" in raw:
        return raw

    return ""


_load_store()

# ===================== Endpoints =====================

@router.get("/views")
async def crm_views(user=Depends(get_current_user)):
    """
    Retorna contagem de registros por estágio.
    """
    counts = {s: 0 for s in STAGES}
    for v in _store.values():
        st = v.get("stage", "lead")
        if st in counts:
            counts[st] += 1
    return {"counts": counts, "stages": STAGES}


@router.get("/list")
async def crm_list(
    stage: str = Query(..., description="Um dos estágios definidos em STAGES"),
    q: Optional[str] = Query(None, description="Filtro por chatid/notes (contém)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    """
    Lista registros do CRM por estágio, com paginação simples.
    """
    if stage not in STAGES:
        raise HTTPException(400, f"Estágio inválido: {stage}")

    items = [v for v in _store.values() if v.get("stage") == stage]

    if q:
        ql = q.lower()
        items = [
            v for v in items
            if ql in (v.get("chatid") or "").lower()
            or ql in (v.get("notes") or "").lower()
        ]

    items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)

    return {
        "items": items[offset: offset + limit],
        "total": len(items),
        "stage": stage,
    }


@router.get("/item")
async def crm_item(chatid: str = Query(...), user=Depends(get_current_user)):
    """
    Retorna (ou cria default em memória) um item do CRM pelo chatid.
    """
    return _store.get(chatid) or {
        "chatid": chatid,
        "stage": "lead",
        "notes": "",
        "updated_at": 0,
    }


@router.post("/status")
async def crm_set_status(
    payload: Dict = Body(..., example={
        "chatid": "55319...@s.whatsapp.net",
        "stage": "lead_qualificado",
        "notes": "",
    }),
    user=Depends(get_current_user),
):
    """
    Define/atualiza o estágio de um chat.
    Aceita chatid/wa_chatid/number.
    """
    stage = (payload.get("stage") or "").strip()
    if stage not in STAGES:
        raise HTTPException(400, f"Estágio inválido: {stage}")

    chatid = _normalize_chatid(
        chatid=payload.get("chatid") or payload.get("wa_chatid"),
        number=payload.get("number"),
    )
    if not chatid:
        raise HTTPException(400, "chatid/wa_chatid/number é obrigatório")

    notes = payload.get("notes", "")
    meta = payload.get("meta") or {}

    rec = _store.get(chatid) or {"chatid": chatid}
    rec.update({
        "stage": stage,
        "notes": notes,
        "meta": meta,
        "updated_at": _now(),
    })
    _store[chatid] = rec
    _save_store()
    return {"ok": True, "item": rec}


@router.delete("/status")
async def crm_clear_status(chatid: str = Query(...), user=Depends(get_current_user)):
    """
    Remove o registro de CRM de um chat.
    """
    if chatid in _store:
        _store.pop(chatid, None)
        _save_store()
    return {"ok": True, "chatid": chatid}


@router.post("/sync")
async def crm_sync_from_uazapi(
    limit_per_page: int = Body(500, embed=True, description="Tamanho da página na UAZAPI (/chat/find)"),
    max_total: int = Body(5000, embed=True, description="Máximo acumulado a buscar"),
    sort: str = Body("-wa_lastMsgTimestamp", embed=True, description="Ordenação da UAZAPI"),
    ctx=Depends(get_uazapi_ctx),
    user=Depends(get_current_user),
):
    """
    Sincroniza com a UAZAPI paginando via /chat/find.
    - Cria registros que não existirem, no estágio 'lead'.
    - Não altera registros já existentes.
    """
    base = f"https://{ctx['host']}"
    headers = {"token": ctx["token"]}
    url = f"{base}/chat/find"

    created = 0
    offset = 0
    total_fetched = 0

    async with httpx.AsyncClient(timeout=90) as cli:
        while total_fetched < max_total:
            body = {
                "operator": "AND",
                "sort": sort,
                "limit": limit_per_page,
                "offset": offset,
            }
            try:
                r = await cli.post(url, json=body, headers=headers)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Erro de rede em /chat/find: {e}")

            if r.status_code >= 400:
                raise HTTPException(
                    status_code=r.status_code,
                    detail=f"Falha ao obter lista de chats da UAZAPI para sincronização: {r.text}",
                )

            try:
                data = r.json()
            except Exception:
                raise HTTPException(status_code=502, detail="Resposta inválida da UAZAPI em /chat/find")

            # normaliza possível forma de retorno
            if isinstance(data, dict):
                items = data.get("items") or data.get("data") or data.get("results") or data.get("chats") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []

            if not isinstance(items, list):
                items = []

            if not items:
                break

            for c in items:
                wa_chatid = (
                    c.get("wa_chatid") or c.get("chatid") or
                    c.get("wa_fastid") or c.get("id")
                )
                chatid = _normalize_chatid(chatid=wa_chatid)
                if not chatid:
                    continue

                if chatid not in _store:
                    _store[chatid] = {
                        "chatid": chatid,
                        "stage": "lead",
                        "notes": "",
                        "meta": {},
                        "updated_at": _now(),
                    }
                    created += 1

            total_fetched += len(items)
            offset += limit_per_page

            if len(items) < limit_per_page:
                break  # última página

    if created:
        _save_store()

    return {"ok": True, "created": created, "total": len(_store), "fetched": total_fetched}


# --------- Helper para outros módulos gravarem status sem Depends ---------
def set_status_internal(chatid: str, stage: str, notes: str = "", meta: Optional[dict] = None):
    """
    Uso interno (ex.: módulo de IA) para gravar estágio diretamente.
    """
    if stage not in STAGES:
        stage = "lead"
    rec = _store.get(chatid) or {"chatid": chatid}
    rec.update({
        "stage": stage,
        "notes": notes or "",
        "meta": meta or {},
        "updated_at": _now(),
    })
    _store[chatid] = rec
    _save_store()
    return rec
