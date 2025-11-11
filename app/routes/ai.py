# app/routes/ai.py
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx  # buscar mensagens na UAZAPI

from app.services.lead_status import (  # type: ignore
    upsert_lead_status,
    should_reclassify,
    get_lead_status,
)

# =========================
# Normalização / utilitários
# =========================
def _normalize_stage(s: str) -> str:
    s = (s or "").strip().lower()
    if s.startswith("contato"):
        return "contatos"
    if "lead_quente" in s or "quente" in s:
        return "lead_quente"
    if s == "lead":
        return "lead"
    return "contatos"


def _to_ms(ts: Optional[int | str]) -> int:
    try:
        n = int(ts or 0)
    except Exception:
        return 0
    if len(str(abs(n))) == 10:  # epoch s
        n *= 1000
    return n


def _is_from_me(m: Dict[str, Any]) -> bool:
    return bool(
        m.get("fromMe")
        or m.get("fromme")
        or m.get("from_me")
        or (isinstance(m.get("key"), dict) and m["key"].get("fromMe"))
        or (
            isinstance(m.get("message"), dict)
            and isinstance(m["message"].get("key"), dict)
            and m["message"]["key"].get("fromMe")
        )
        or (isinstance(m.get("sender"), dict) and m["sender"].get("fromMe"))
        or (isinstance(m.get("id"), str) and m["id"].startswith("true_"))
        or m.get("user") == "me"
    )


def _text_of(m: Dict[str, Any]) -> str:
    mm = m.get("message") or {}
    for path in (
        ("text",),
        ("caption",),
        ("body",),
        ("message", "text"),
        ("message", "conversation"),
        ("message", "extendedTextMessage", "text"),
        ("message", "imageMessage", "caption"),
        ("message", "videoMessage", "caption"),
        ("message", "documentMessage", "caption"),
    ):
        cur: Any = m
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def _ts_of(m: Dict[str, Any]) -> int:
    return _to_ms(
        m.get("messageTimestamp")
        or m.get("timestamp")
        or m.get("t")
        or (m.get("message") or {}).get("messageTimestamp")
        or 0
    )


# =========================
# Regras de classificação (refinadas)
# =========================
# 1) Todo mundo começa "contatos".
# 2) "lead" quando o CLIENTE aceita/autoriza seguir
#    (botão/lista com "sim", "pode continuar", "quero saber mais"... ou texto equivalente).
# 3) "lead_quente" SOMENTE quando NÓS (fromMe=True) falamos em
#    encaminhar/transferir/colocar em contato/passar número etc.

YES_TOKENS = (
    "sim",
    "pode continuar",
    "quero saber mais",
    "pode prosseguir",
    "seguir",
    "continuar",
    "ok pode",
    "ok, pode",
    "ok pode continuar",
    "aceito",
    "autorizo",
)

# Regex balanceada cobrindo ações nossas (encaminhar/transferir etc.)
HOT_ACTION_PAT = re.compile(
    r"(?:\b(?:vou|vamos)\s*(?:te\s*)?(?:encaminhar|transferir|direcionar)\b"
    r"|(?:vou|vamos)\s*(?:te\s*)?(?:colocar|por)\s*(?:em|no)\s*contato\b"
    r"|(?:vou|vamos)\s*passar\s*(?:seu|o)\s*n[uú]mero\b"
    r"|(?:vou|vamos)\s*(?:te\s*)?passar\s*(?:para|pra)\s*(?:o|a)\s*setor\b"
    r"|(?:te\s*)?coloco\s*(?:em|no)\s*contato\b"
    r"|vou\s*agendar\b"
    r"|vou\s*marcar\b"
    r"|vou\s*abrir\s*chamado\b)",
    re.IGNORECASE,
)


def _is_interactive_yes(m: Dict[str, Any]) -> bool:
    msg = m.get("message") or {}
    # respostas de botões
    br = msg.get("buttonsResponseMessage") or {}
    if any(tok in (br.get("selectedDisplayText") or "").lower() for tok in YES_TOKENS):
        return True
    if any(tok in (br.get("selectedButtonId") or "").lower() for tok in YES_TOKENS):
        return True
    # respostas de listas
    lr = msg.get("listResponseMessage") or {}
    single = (lr.get("singleSelectReply") or {})
    if any(tok in (single.get("selectedRowId") or "").lower() for tok in YES_TOKENS):
        return True
    if any(tok in (lr.get("title") or "").lower() for tok in YES_TOKENS):
        return True
    return False


def _stage_from_messages(messages: List[Dict[str, Any]]) -> tuple[str, int, bool]:
    """
    Retorna (stage, last_msg_ts_ms, last_from_me)
    Regras:
      - procura de trás pra frente:
        1) se encontrarmos UMA mensagem NOSSA que contenha ação de encaminhamento => lead_quente
        2) senão, se encontrarmos UMA mensagem do CLIENTE aceitando (botão/lista) OU texto com YES_TOKENS => lead
        3) caso contrário => contatos
    """
    if not messages:
        return "contatos", 0, False

    msgs = messages[-60:] if len(messages) > 60 else messages

    try:
        last = max(msgs, key=_ts_of)
    except ValueError:
        last = msgs[-1]
    last_ts = _ts_of(last)
    last_from_me = _is_from_me(last)

    # 1) nossa ação explícita => lead_quente
    for m in reversed(msgs):
        if _is_from_me(m):
            txt = _text_of(m)
            if txt and HOT_ACTION_PAT.search(txt):
                return "lead_quente", last_ts, last_from_me

    # 2) aceite do cliente => lead
    for m in reversed(msgs):
        if not _is_from_me(m):
            if _is_interactive_yes(m):
                return "lead", last_ts, last_from_me
            txt = _text_of(m).lower()
            if any(tok in txt for tok in YES_TOKENS):
                return "lead", last_ts, last_from_me

    # 3) default
    return "contatos", last_ts, last_from_me


# =========================
# API pública (usada pelas rotas)
# =========================
async def classify_chat(
    chatid: str,
    persist: bool = True,
    limit: int = 200,
    ctx: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Retorna {"stage": "..."} usando:
    - Banco (se existir e NÃO precisar reclassificar);
    - Caso contrário, busca mensagens na UAZAPI, aplica regra e persiste (se persist=True).
    """
    ctx = ctx or {}
    instance_id = str(
        ctx.get("instance_id")
        or ctx.get("phone_number_id")
        or ctx.get("pnid")
        or ctx.get("sub")
        or ""
    )

    # 1) tenta banco
    try:
        cur = await get_lead_status(instance_id, chatid)
    except Exception:
        cur = None

    need_reclass = True
    if cur and cur.get("stage"):
        try:
            need_reclass = await should_reclassify(
                instance_id,
                chatid,
                last_msg_ts=None,
                last_from_me=None,
            )
        except Exception:
            need_reclass = False

        if not need_reclass:
            return {"stage": _normalize_stage(str(cur["stage"]))}

    # 2) busca mensagens na UAZAPI e aplica regras
    base = f"https://{ctx['host']}"
    headers = {"token": ctx["token"]}
    payload = {"chatid": chatid, "limit": int(limit or 200), "offset": 0, "sort": "-messageTimestamp"}

    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.post(f"{base}/message/find", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()

    items: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            items = data["items"]
        else:
            for k in ("data", "results", "messages"):
                v = data.get(k)
                if isinstance(v, list):
                    items = v
                    break
    elif isinstance(data, list):
        items = data

    stage, last_ts, last_from_me = _stage_from_messages(items)
    stage = _normalize_stage(stage)

    if persist:
        try:
            await upsert_lead_status(
                instance_id,
                chatid,
                stage,
                last_msg_ts=int(last_ts or 0),
                last_from_me=bool(last_from_me),
            )
        except Exception:
            pass

    return {"stage": stage}


# Compat: alguns módulos importam esse nome
async def classify_stage(
    chatid: str,
    persist: bool = True,
    limit: int = 200,
    ctx: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return await classify_chat(chatid=chatid, persist=persist, limit=limit, ctx=ctx)


# Usado por app/routes/media.py (quando já temos as mensagens)
async def classify_by_rules(
    messages: List[Dict[str, Any]] | None = None,
    chatid: Optional[str] = None,
    ctx: Dict[str, Any] | None = None,
    persist: bool = True,
) -> Dict[str, Any]:
    msgs = messages or []
    stage, last_ts, last_from_me = _stage_from_messages(msgs)
    stage = _normalize_stage(stage)

    if persist and chatid:
        instance_id = str(
            (ctx or {}).get("instance_id")
            or (ctx or {}).get("phone_number_id")
            or (ctx or {}).get("pnid")
            or (ctx or {}).get("sub")
            or ""
        )
        try:
            await upsert_lead_status(
                instance_id,
                chatid,
                stage,
                last_msg_ts=int(last_ts or 0),
                last_from_me=bool(last_from_me),
            )
        except Exception:
            pass

    return {"stage": stage}
