from __future__ import annotations
from typing import Dict, Any, List, Optional
import inspect
import httpx

from fastapi import APIRouter, Depends, HTTPException, Body, Request

from app.routes.deps import get_uazapi_ctx
# USAR guard tolerante aqui também
from app.routes.deps_billing import require_active_tenant_soft

# lead_status (persistência)
from app.services.lead_status import (  # type: ignore
    get_lead_status,
    upsert_lead_status,
    should_reclassify,
)

# >>> NOVO: persistência de mensagens (não bloqueante)
try:
    from app.services.messages import bulk_upsert_messages  # type: ignore
except Exception:
    bulk_upsert_messages = None  # se faltar o módulo, só segue

# --- tenta usar a regra oficial do módulo ai.py; se não existir, usa fallback local
try:
    from app.routes.ai import classify_stage as _ai_classify_stage  # type: ignore
except Exception:
    _ai_classify_stage = None  # sem dependência dura

router = APIRouter()

# ============================================================================
# NOVO: LISTAR CONVERSAS (CHATS)
# ============================================================================

@router.get("/chats/list")
async def list_chats(
    request: Request,
    _user=Depends(require_active_tenant_soft),
    ctx=Depends(get_uazapi_ctx),
):
    """
    Lista todas as conversas (chats) de uma instância.
    
    Retorna:
    - chat_id: ID do chat
    - last_message: Última mensagem recebida
    - last_timestamp: Timestamp da última mensagem
    - unread_count: Quantidade de mensagens não lidas (TODO)
    - contact_name: Nome do contato (buscar via API)
    """
    instance_id = _get_instance_id_from_request(request)
    
    if not instance_id:
        raise HTTPException(status_code=400, detail="instance_id não encontrado")
    
    try:
        # Buscar todas as conversas do banco
        from app.pg import get_pool
        
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Buscar chats distintos com última mensagem
                cur.execute("""
                    SELECT 
                        m.chat_id,
                        MAX(m.timestamp) as last_timestamp,
                        (
                            SELECT content 
                            FROM messages m2 
                            WHERE m2.chat_id = m.chat_id 
                            AND m2.instance_id = m.instance_id
                            ORDER BY m2.timestamp DESC 
                            LIMIT 1
                        ) as last_message,
                        (
                            SELECT from_me 
                            FROM messages m2 
                            WHERE m2.chat_id = m.chat_id 
                            AND m2.instance_id = m.instance_id
                            ORDER BY m2.timestamp DESC 
                            LIMIT 1
                        ) as last_from_me,
                        COUNT(*) as message_count
                    FROM messages m
                    WHERE m.instance_id = %s
                    GROUP BY m.chat_id
                    ORDER BY MAX(m.timestamp) DESC
                    LIMIT 100
                """, (instance_id,))
                
                rows = cur.fetchall()
                
                chats = []
                for row in rows:
                    chat_id = row[0]
                    last_timestamp = row[1]
                    last_message = row[2] or ""
                    last_from_me = row[3]
                    message_count = row[4]
                    
                    chats.append({
                        "chat_id": chat_id,
                        "last_timestamp": int(last_timestamp) if last_timestamp else 0,
                        "last_message": last_message[:100],  # Prévia
                        "last_from_me": bool(last_from_me),
                        "message_count": message_count,
                        "unread_count": 0,  # TODO: implementar
                    })
                
                return {"items": chats, "total": len(chats)}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao listar chats: {str(e)}")


# ---------------- util: extrai instance_id do JWT/headers ---------------- #
def _b64url_to_bytes(s: str) -> bytes:
    import base64
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
                import json as _json
                payload = _json.loads(_b64url_to_bytes(parts[1]).decode("utf-8"))
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


def _uaz(ctx):
    base = f"https://{ctx['host']}"
    headers = {"token": ctx["token"]}
    return base, headers


def _normalize_items(resp_json):
    if isinstance(resp_json, dict):
        if isinstance(resp_json.get("items"), list):
            return {"items": resp_json["items"]}
        for key in ("data", "results", "messages"):
            val = resp_json.get(key)
            if isinstance(val, list):
                return {"items": val}
        return {"items": []}
    if isinstance(resp_json, list):
        return {"items": resp_json}
    return {"items": []}


# -------- fallback simples ----------
def _fallback_classify_stage(items: List[Dict[str, Any]]) -> str:
    HOT = ("fechar", "fechamos", "pix", "pagar", "preço", "valor", "contrato", "assinar")
    text_fields = (
        "text",
        "caption",
        "body",
        ("message", "text"),
        ("message", "conversation"),
        ("message", "extendedTextMessage", "text"),
    )

    def _get_text(m: Dict[str, Any]) -> str:
        for f in text_fields:
            if isinstance(f, str):
                v = m.get(f)
            else:
                v = m
                for k in f:
                    if not isinstance(v, dict):
                        v = None
                        break
                    v = v.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
        return ""

    hot = any(any(tok in _get_text(m) for tok in HOT) for m in items)
    if hot:
        return "lead_quente"
    if items:
        return "lead"
    return "contatos"


async def _classify_stage(items: List[Dict[str, Any]]) -> str:
    if _ai_classify_stage:
        try:
            res = _ai_classify_stage(items)
            if inspect.isawaitable(res):
                res = await res  # type: ignore[func-returns-value]
            if isinstance(res, dict):
                stage = res.get("stage")
                if isinstance(stage, str):
                    return stage
            if isinstance(res, str) and res:
                return res
        except Exception:
            pass
    return _fallback_classify_stage(items)


# -------- helpers p/ ts e autoria ----------
def _ts_of(m: Dict[str, Any]) -> int:
    ts = m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or m.get("message", {}).get("messageTimestamp") or 0
    try:
        n = int(ts)
    except Exception:
        return 0
    if len(str(n)) == 10:
        n *= 1000
    return n

def _is_from_me(m: Dict[str, Any]) -> bool:
    return bool(
        m.get("fromMe")
        or m.get("fromme")
        or m.get("from_me")
        or (isinstance(m.get("key"), dict) and m["key"].get("fromMe"))
        or (isinstance(m.get("message"), dict) and isinstance(m["message"].get("key"), dict) and m["message"]["key"].get("fromMe"))
        or (isinstance(m.get("sender"), dict) and m["sender"].get("fromMe"))
        or (isinstance(m.get("id"), str) and m["id"].startswith("true_"))
        or m.get("user") == "me"
    )


@router.post("/messages")
async def find_messages(
    request: Request,
    body: dict | None = Body(None),
    _user=Depends(require_active_tenant_soft),  # <<< guard tolerante
    ctx=Depends(get_uazapi_ctx),
):
    """
    Proxy para UAZAPI /message/find.
    Além de devolver normalizado, calcula `stage` e PERSISTE em lead_status quando:
      - não há registro no banco, ou
      - há mensagem mais recente / mudança de autoria (precisa reclassificar).

    >>> NOVO:
      - Persiste as mensagens em `public.messages` (bulk upsert) quando possível.
        Essa persistência é best-effort e NÃO bloqueia a resposta.
    """
    if not body or not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body inválido")

    chatid = body.get("chatid")
    if not chatid or not isinstance(chatid, str) or not chatid.strip():
        raise HTTPException(status_code=400, detail="chatid é obrigatório")
    chatid = chatid.strip()

    instance_id = _get_instance_id_from_request(request)

    base, headers = _uaz(ctx)
    url = f"{base}/message/find"

    payload = {
        "chatid": chatid,
        "limit": int(body.get("limit") or 200),
        "offset": int(body.get("offset") or 0),
        "sort": body.get("sort") or "-messageTimestamp",
    }

    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(url, json=payload, headers=headers)

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Resposta inválida da UAZAPI em /message/find")

    wrapped = _normalize_items(data)
    items: List[Dict[str, Any]] = wrapped["items"]

    # calcula últimos marcadores
    last_ts = max((_ts_of(m) for m in items), default=0)
    last_from_me = _is_from_me(items[-1]) if items else False

    # verifica banco e decide reclassificação
    stage: Optional[str] = None
    try:
        rec = await get_lead_status(instance_id, chatid) if instance_id else None
    except Exception:
        rec = None

    need_reclass = True
    if rec and rec.get("stage"):
        try:
            need_reclass = await should_reclassify(
                instance_id, chatid, last_msg_ts=last_ts, last_from_me=last_from_me
            )
        except Exception:
            need_reclass = False
        if not need_reclass:
            stage = str(rec["stage"])

    if stage is None:
        stage = await _classify_stage(items)
        if instance_id:
            try:
                await upsert_lead_status(
                    instance_id, chatid, stage, last_msg_ts=int(last_ts or 0), last_from_me=bool(last_from_me)
                )
            except Exception:
                pass

    # >>> NOVO: persistência best-effort das mensagens (não bloqueia)
    if bulk_upsert_messages and instance_id and items:
        try:
            _ = await bulk_upsert_messages(instance_id, chatid, items)
        except Exception:
            pass

    return {"items": items, "stage": stage}
