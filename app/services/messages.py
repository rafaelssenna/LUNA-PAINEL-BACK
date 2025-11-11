from __future__ import annotations
from typing import Any, Dict, Iterable, List, Tuple, Optional

from app.pg import get_pool


# ---- Normalizadores (id, ts, from_me, texto, mídia) -----------------------

def _extract_msgid(m: Dict[str, Any]) -> Optional[str]:
    """
    Tenta identificar o id da mensagem em diferentes formatos comuns.
    """
    # campos diretos
    for k in ("id", "msgid", "messageId", "wa_msgid", "wa_message_id"):
        v = m.get(k)
        if isinstance(v, str) and v:
            return v

    # aninhados comuns
    key = m.get("key")
    if isinstance(key, dict):
        v = key.get("id")
        if isinstance(v, str) and v:
            return v

    message = m.get("message")
    if isinstance(message, dict):
        k = message.get("key")
        if isinstance(k, dict):
            v = k.get("id")
            if isinstance(v, str) and v:
                return v

    return None


def _extract_ts(m: Dict[str, Any]) -> int:
    ts = (
        m.get("messageTimestamp")
        or m.get("timestamp")
        or m.get("t")
        or (isinstance(m.get("message"), dict) and m["message"].get("messageTimestamp"))
        or 0
    )
    try:
        n = int(ts)
    except Exception:
        return 0
    if len(str(n)) == 10:
        n *= 1000
    return n


def _extract_from_me(m: Dict[str, Any]) -> bool:
    if m.get("fromMe") or m.get("fromme") or m.get("from_me"):
        return True
    key = m.get("key")
    if isinstance(key, dict) and key.get("fromMe"):
        return True
    msg = m.get("message")
    if isinstance(msg, dict):
        mk = msg.get("key")
        if isinstance(mk, dict) and mk.get("fromMe"):
            return True
    if isinstance(m.get("id"), str) and str(m["id"]).startswith("true_"):
        return True
    return m.get("user") == "me"


def _extract_text(m: Dict[str, Any]) -> Optional[str]:
    text_fields = (
        "text",
        "caption",
        "body",
        ("message", "text"),
        ("message", "conversation"),
        ("message", "extendedTextMessage", "text"),
    )
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
            return v
    return None


def _extract_media(m: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    # heurística simples
    url = m.get("mediaUrl") or m.get("url") or m.get("media_url")
    mime = m.get("mimetype") or m.get("mime") or m.get("media_mime")
    if isinstance(url, str) and not url:
        url = None
    if isinstance(mime, str) and not mime:
        mime = None
    return url, mime


# ---- Persistência ----------------------------------------------------------

async def bulk_upsert_messages(
    instance_id: str,
    chatid: str,
    items: Iterable[Dict[str, Any]],
) -> int:
    """
    Insere/atualiza mensagens no storage local (best-effort).
    Retorna quantidade de linhas afetadas (aproximado).
    """
    rows: List[Tuple[str, str, str, bool, int, Optional[str], Optional[str], Optional[str]]] = []

    for m in items:
        msgid = _extract_msgid(m)
        if not msgid:
            # não insere se não conseguir identificar o id
            continue
        ts = _extract_ts(m)
        from_me = _extract_from_me(m)
        text = _extract_text(m)
        media_url, media_mime = _extract_media(m)

        rows.append((
            instance_id,
            chatid,
            msgid,
            from_me,
            ts,
            text,
            media_url,
            media_mime,
        ))

    if not rows:
        return 0

    sql = """
    INSERT INTO messages
      (instance_id, chatid, msgid, from_me, ts, text, media_url, media_mime)
    VALUES %s
    ON CONFLICT (instance_id, chatid, msgid) DO UPDATE
      SET from_me   = EXCLUDED.from_me,
          ts        = GREATEST(messages.ts, EXCLUDED.ts),
          text      = COALESCE(EXCLUDED.text, messages.text),
          media_url = COALESCE(EXCLUDED.media_url, messages.media_url),
          media_mime= COALESCE(EXCLUDED.media_mime, messages.media_mime);
    """

    # psycopg3: compose VALUES com executemany-like
    # usaremos formatação manual segura com placeholders
    placeholders = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s)"] * len(rows))
    sql_exec = sql.replace("%s", placeholders, 1)

    flat: List[Any] = []
    for r in rows:
        flat.extend(r)

    with get_pool().connection() as con:
        con.execute(sql_exec, flat)

    return len(rows)
