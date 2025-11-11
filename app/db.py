# app/db.py
import os, sqlite3, hashlib, time
from threading import RLock

DB_PATH = os.getenv("LUNA_DB_PATH", os.path.join(os.getcwd(), "data", "luna.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_lock = RLock()

def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""
        CREATE TABLE IF NOT EXISTS crm_status (
            instance TEXT NOT NULL,
            chatid   TEXT NOT NULL,
            stage    TEXT NOT NULL,
            notes    TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (instance, chatid)
        )
    """)
    return con

_con = _connect()

def inst_key(token: str) -> str:
    # não guardamos o token em texto puro; usamos hash
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def upsert_status(instance: str, chatid: str, stage: str, notes: str | None = None):
    now = int(time.time())
    with _lock:
        _con.execute("""
            INSERT INTO crm_status (instance, chatid, stage, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(instance, chatid) DO UPDATE SET
              stage=excluded.stage, notes=excluded.notes, updated_at=excluded.updated_at
        """, (instance, chatid, stage, notes, now))
        _con.commit()

def get_status(instance: str, chatid: str) -> dict | None:
    cur = _con.execute("SELECT stage, notes, updated_at FROM crm_status WHERE instance=? AND chatid=?",
                       (instance, chatid))
    row = cur.fetchone()
    if not row: return None
    return {"stage": row[0], "notes": row[1], "updated_at": row[2]}

def list_by_stage(instance: str, stage: str, limit: int = 50, offset: int = 0):
    cur = _con.execute("""
        SELECT chatid, stage, notes, updated_at
        FROM crm_status
        WHERE instance=? AND stage=?
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?
    """, (instance, stage, limit, offset))
    rows = cur.fetchall()
    return [{"chatid": r[0], "stage": r[1], "notes": r[2], "updated_at": r[3]}] if rows else []

def counts_by_stage(instance: str) -> dict:
    cur = _con.execute("""
        SELECT stage, COUNT(*) FROM crm_status
        WHERE instance=?
        GROUP BY stage
    """, (instance,))
    return {row[0]: row[1] for row in cur.fetchall()}
