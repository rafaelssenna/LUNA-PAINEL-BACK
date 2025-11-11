from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import bcrypt
from app.pg import get_pool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_user(email: str, password: str) -> Dict[str, Any]:
    email = email.strip().lower()
    pwd_hash = hash_password(password)
    sql = """
      INSERT INTO users (email, password_hash, created_at)
      VALUES (%s, %s, NOW())
      ON CONFLICT (email) DO NOTHING
      RETURNING id, email, created_at, last_login_at;
    """
    with get_pool().connection() as con:
        row = con.execute(sql, (email, pwd_hash)).fetchone()  # já é dict
    if not row:
        raise ValueError("E-mail já cadastrado")
    return row


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    sql = """
      SELECT id, email, password_hash, created_at, last_login_at
      FROM users
      WHERE email=%s
    """
    with get_pool().connection() as con:
        row = con.execute(sql, (email.strip().lower(),)).fetchone()
    return row if row else None  # já é dict


def touch_last_login(user_id: int) -> None:
    with get_pool().connection() as con:
        con.execute("UPDATE users SET last_login_at=NOW() WHERE id=%s", (user_id,))
