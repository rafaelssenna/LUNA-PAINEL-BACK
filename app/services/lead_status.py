# app/services/lead_status.py
from __future__ import annotations
from typing import Iterable, List, Optional, Dict, Any, Tuple

from starlette.concurrency import run_in_threadpool
from app.pg import get_pool

# Tabela esperada:
# lead_status(
#   instance_id TEXT, chatid TEXT,
#   stage TEXT, updated_at TIMESTAMPTZ,
#   last_msg_ts BIGINT, last_from_me BOOLEAN,
#   PRIMARY KEY(instance_id, chatid)
# )

# ---------- helpers ----------

def _row_to_dict(row: Tuple) -> Dict[str, Any]:
    # row order must match SELECT order abaixo
    if not row:
        return {}
    instance_id, chatid, stage, updated_at, last_msg_ts, last_from_me = row
    return {
        "instance_id": instance_id,
        "chatid": chatid,
        "stage": _normalize_stage(stage),
        "updatedAt": updated_at.isoformat() if updated_at else None,
        "last_msg_ts": int(last_msg_ts or 0),
        "last_from_me": bool(last_from_me),
    }

def _normalize_stage(stage: str) -> str:
    s = (stage or "").strip().lower()
    if s.startswith("contato"):
        return "contatos"
    if "lead_quente" in s or "quente" in s:
        return "lead_quente"
    if s == "lead":
        return "lead"
    return "contatos"

def _canon_forms(cid: str) -> List[str]:
    """
    Retorna as duas formas equivalentes de chatid:
    - sem sufixo
    - com sufixo @s.whatsapp.net
    """
    c = (cid or "").strip()
    if not c:
        return []
    if "@s.whatsapp.net" in c:
        base = c.replace("@s.whatsapp.net", "")
        return [base, c]
    # se já parecer um grupo (g.us) ou outro domínio, mantém
    if "@g.us" in c or "@" in c:
        return [c]
    return [c, f"{c}@s.whatsapp.net"]

def _base_key(cid: str) -> str:
    """Chave lógica p/ agrupar 5511... e 5511...@s.whatsapp.net."""
    c = (cid or "").strip()
    if c.endswith("@s.whatsapp.net"):
        return c[:-len("@s.whatsapp.net")]
    return c

def _guess_store_chatid(con, instance_id: str, chatid: str) -> str:
    """
    Escolhe qual chatid usar para armazenar:
    - se já existe registro para alguma forma, usa exatamente a que existe
    - senão, se veio sem domínio e é só dígito, usa com @s.whatsapp.net
    - fallback: usa o recebido
    """
    forms = _canon_forms(chatid)
    if not forms:
        return chatid
    placeholders = ", ".join(["%s"] * len(forms))
    with con.cursor() as cur:
        cur.execute(
            f"""
            SELECT chatid
              FROM lead_status
             WHERE instance_id = %s AND chatid IN ({placeholders})
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            (instance_id, *forms),
        )
        r = cur.fetchone()
        if r and r[0]:
            return str(r[0])

    base = chatid.strip()
    if ("@" not in base) and base.isdigit():
        return f"{base}@s.whatsapp.net"
    return chatid

# ---------- SYNC core (psycopg pool síncrono) ----------

def _get_lead_status_sync(instance_id: str, chatid: str) -> Optional[Dict[str, Any]]:
    pool = get_pool()
    ids = _canon_forms(chatid)
    if not ids:
        return None
    placeholders = ", ".join(["%s"] * len(ids))
    with pool.connection() as con:
        with con.cursor() as cur:
            cur.execute(
                f"""
                SELECT instance_id, chatid, stage, updated_at, last_msg_ts, last_from_me
                  FROM lead_status
                 WHERE instance_id = %s AND chatid IN ({placeholders})
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (instance_id, *ids),
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None

def _get_many_lead_status_sync(instance_id: str, chatids: Iterable[str]) -> List[Dict[str, Any]]:
    base_ids = [c for c in dict.fromkeys(chatids) if c]
    if not base_ids:
        return []

    # expande cada id para formas canônicas e dedup
    all_ids: List[str] = []
    for c in base_ids:
        all_ids.extend(_canon_forms(c))
    all_ids = list(dict.fromkeys(all_ids))
    if not all_ids:
        return []

    placeholders = ", ".join(["%s"] * len(all_ids))
    params: Tuple[Any, ...] = (instance_id, *all_ids)

    pool = get_pool()
    with pool.connection() as con:
        with con.cursor() as cur:
            cur.execute(
                f"""
                SELECT instance_id, chatid, stage, updated_at, last_msg_ts, last_from_me
                  FROM lead_status
                 WHERE instance_id = %s AND chatid IN ({placeholders})
                """,
                params,
            )
            rows = cur.fetchall() or []

    # Pode vir duplicado (ex.: 5511... e 5511...@s.whatsapp.net). Mantém o mais recente por base_key.
    best_by_base: Dict[str, Tuple] = {}
    for r in rows:
        # r = (instance_id, chatid, stage, updated_at, last_msg_ts, last_from_me)
        chatid = r[1]
        updated_at = r[3]
        base = _base_key(chatid)
        cur = best_by_base.get(base)
        if (cur is None) or (updated_at and cur[3] and updated_at > cur[3]):
            best_by_base[base] = r

    return [_row_to_dict(r) for r in best_by_base.values()]

def _upsert_lead_status_sync(
    instance_id: str,
    chatid: str,
    stage: str,
    last_msg_ts: int = 0,
    last_from_me: bool = False,
) -> Dict[str, Any]:
    s = _normalize_stage(stage)
    pool = get_pool()
    with pool.connection() as con:
        store_chatid = _guess_store_chatid(con, instance_id, chatid)
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lead_status (instance_id, chatid, stage, updated_at, last_msg_ts, last_from_me)
                VALUES (%s, %s, %s, NOW(), %s, %s)
                ON CONFLICT (instance_id, chatid)
                DO UPDATE SET
                  stage = EXCLUDED.stage,
                  updated_at = NOW(),
                  last_msg_ts = GREATEST(lead_status.last_msg_ts, EXCLUDED.last_msg_ts),
                  last_from_me = EXCLUDED.last_from_me
                RETURNING instance_id, chatid, stage, updated_at, last_msg_ts, last_from_me
                """,
                (instance_id, store_chatid, s, int(last_msg_ts or 0), bool(last_from_me)),
            )
            row = cur.fetchone()
    return _row_to_dict(row)

def _should_reclassify_sync(
    instance_id: str,
    chatid: str,
    last_msg_ts: Optional[int] = None,
    last_from_me: Optional[bool] = None,
) -> bool:
    cur = _get_lead_status_sync(instance_id, chatid)
    if not cur:
        return True
    if last_msg_ts and int(last_msg_ts) > int(cur.get("last_msg_ts") or 0):
        return True
    if last_from_me is not None and bool(last_from_me) != bool(cur.get("last_from_me")):
        return True
    return False

# ---------- Wrappers assíncronos ----------

async def get_lead_status(instance_id: str, chatid: str) -> Optional[Dict[str, Any]]:
    return await run_in_threadpool(_get_lead_status_sync, instance_id, chatid)

async def get_many_lead_status(instance_id: str, chatids: Iterable[str]) -> List[Dict[str, Any]]:
    return await run_in_threadpool(_get_many_lead_status_sync, instance_id, chatids)

async def upsert_lead_status(
    instance_id: str,
    chatid: str,
    stage: str,
    last_msg_ts: int = 0,
    last_from_me: bool = False,
) -> Dict[str, Any]:
    return await run_in_threadpool(
        _upsert_lead_status_sync, instance_id, chatid, stage, last_msg_ts, last_from_me
    )

async def should_reclassify(
    instance_id: str,
    chatid: str,
    last_msg_ts: Optional[int] = None,
    last_from_me: Optional[bool] = None,
) -> bool:
    return await run_in_threadpool(
        _should_reclassify_sync, instance_id, chatid, last_msg_ts, last_from_me
    )

# ---------- Aliases de compatibilidade ----------

async def getCachedLeadStatus(instance_id: str, chatid: str) -> Optional[Dict[str, Any]]:
    return await get_lead_status(instance_id, chatid)

async def getCachedLeadStatusBulk(instance_id: str, chatids: Iterable[str]) -> List[Dict[str, Any]]:
    return await get_many_lead_status(instance_id, chatids)

async def upsertLeadStatus(
    instance_id: str,
    chatid: str,
    stage: str,
    last_msg_ts: int = 0,
    last_from_me: bool = False,
) -> Dict[str, Any]:
    return await upsert_lead_status(instance_id, chatid, stage, last_msg_ts, last_from_me)

async def needsReclassify(
    instance_id: str,
    chatid: str,
    last_msg_ts: Optional[int] = None,
    last_from_me: Optional[bool] = None,
) -> bool:
    return await should_reclassify(instance_id, chatid, last_msg_ts, last_from_me)

__all__ = [
    # oficiais
    "get_lead_status", "get_many_lead_status", "upsert_lead_status", "should_reclassify",
    # compat + bulk
    "getCachedLeadStatus", "getCachedLeadStatusBulk", "upsertLeadStatus", "needsReclassify",
]
