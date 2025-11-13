from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List
import logging

log = logging.getLogger("uvicorn.error")

# -----------------------------------------------------------------------------
# Backend de armazenamento:
#   - "memory" (default): guarda em memória (volátil, reinício perde dados)
#   - "sqlite": persistência local em arquivo SQLite (sem libs extras)
#
# Variáveis de ambiente:
#   INSTANCES_STORE         -> "memory" | "sqlite"    (default: "memory")
#   INSTANCES_SQLITE_PATH   -> caminho do arquivo .db (default: ./data/instances.db)
# -----------------------------------------------------------------------------
_STORE_BACKEND = os.getenv("INSTANCES_STORE", os.getenv("LUNA_INSTANCES_STORE", "memory")).lower()

_lock = threading.Lock()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _norm_token(token: str) -> str:
    return (token or "").strip()

# -----------------------------------------------------------------------------
# Inicialização do storage
# -----------------------------------------------------------------------------
if _STORE_BACKEND == "sqlite":
    DB_PATH = os.getenv("INSTANCES_SQLITE_PATH", os.path.join(os.getcwd(), "data", "instances.db"))
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, token)
        )
    """)
    _conn.commit()
    log.info("Instances storage: sqlite -> %s", DB_PATH)
else:
    # Estrutura: { user_id: { token: created_at_iso } }
    _mem: Dict[int, Dict[str, str]] = {}
    log.info("Instances storage: memory (volátil)")

# -----------------------------------------------------------------------------
# API pública do serviço
# -----------------------------------------------------------------------------
def attach_instance_to_user(user_id: int, instance_token: str) -> Dict[str, Any]:
    """
    Vincula uma instância (por token) a um usuário.
    - Se já existir vínculo (user_id, token), não cria duplicado e marca "existing": True.
    - Retorna dict com informações do vínculo.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id inválido")

    token = _norm_token(instance_token)
    if not token or len(token) < 4:
        raise ValueError("instance_token inválido")

    ts = _now_iso()

    if _STORE_BACKEND == "sqlite":
        with _lock:
            cur = _conn.cursor()
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO instances (user_id, token, created_at) VALUES (?, ?, ?)",
                    (user_id, token, ts),
                )
                _conn.commit()

                if cur.rowcount == 0:
                    # Já existia (user_id, token)
                    cur.execute(
                        "SELECT id, created_at FROM instances WHERE user_id=? AND token=?",
                        (user_id, token),
                    )
                    row = cur.fetchone()
                    if not row:
                        # Estado inconsistente (raro). Tenta inserir novamente sem IGNORE.
                        cur.execute(
                            "INSERT INTO instances (user_id, token, created_at) VALUES (?, ?, ?)",
                            (user_id, token, ts),
                        )
                        _conn.commit()
                        return {
                            "persisted": True,
                            "existing": False,
                            "backend": "sqlite",
                            "id": cur.lastrowid,
                            "user_id": user_id,
                            "token": token,
                            "created_at": ts,
                        }
                    return {
                        "persisted": True,
                        "existing": True,
                        "backend": "sqlite",
                        "id": row[0],
                        "user_id": user_id,
                        "token": token,
                        "created_at": row[1],
                    }

                # Inseriu agora
                return {
                    "persisted": True,
                    "existing": False,
                    "backend": "sqlite",
                    "id": cur.lastrowid,
                    "user_id": user_id,
                    "token": token,
                    "created_at": ts,
                }
            finally:
                cur.close()

    # memory
    with _lock:
        bucket = _mem.setdefault(user_id, {})
        existing = token in bucket
        if not existing:
            bucket[token] = ts
        return {
            "persisted": True,
            "existing": existing,
            "backend": "memory",
            "user_id": user_id,
            "token": token,
            "created_at": bucket[token],
        }

def get_instances_by_user(user_id: int) -> List[Dict[str, Any]]:
    """
    Lista instâncias vinculadas a um usuário.
    """
    if _STORE_BACKEND == "sqlite":
        with _lock:
            cur = _conn.cursor()
            try:
                cur.execute(
                    "SELECT id, token, created_at FROM instances WHERE user_id=? ORDER BY created_at DESC",
                    (user_id,),
                )
                rows = cur.fetchall()
                return [
                    {"id": r[0], "user_id": user_id, "token": r[1], "created_at": r[2], "backend": "sqlite"}
                    for r in rows
                ]
            finally:
                cur.close()

    # memory
    with _lock:
        bucket = _mem.get(user_id, {})
        return [
            {"user_id": user_id, "token": t, "created_at": created, "backend": "memory"}
            for t, created in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)
        ]

def detach_instance_from_user(user_id: int, instance_token: str) -> bool:
    """
    Remove a associação (user_id, token). Retorna True se removeu, False se não existia.
    """
    token = _norm_token(instance_token)
    if not token:
        return False

    if _STORE_BACKEND == "sqlite":
        with _lock:
            cur = _conn.cursor()
            try:
                cur.execute(
                    "DELETE FROM instances WHERE user_id=? AND token=?",
                    (user_id, token),
                )
                _conn.commit()
                return cur.rowcount > 0
            finally:
                cur.close()

    # memory
    with _lock:
        bucket = _mem.get(user_id, {})
        if token in bucket:
            del bucket[token]
            return True
        return False

def count_instances(user_id: int) -> int:
    """
    Conta quantas instâncias o usuário possui.
    """
    if _STORE_BACKEND == "sqlite":
        with _lock:
            cur = _conn.cursor()
            try:
                cur.execute("SELECT COUNT(*) FROM instances WHERE user_id=?", (user_id,))
                (n,) = cur.fetchone() or (0,)
                return int(n)
            finally:
                cur.close()

    # memor
    with _lock:
        return len(_mem.get(user_id, {}))
