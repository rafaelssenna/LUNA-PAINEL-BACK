# app/models_billing.py
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from datetime import datetime, timedelta, timezone
from app.pg import get_pool  # Usa o pool existente do sistema


# ---------- SCHEMA ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    id           UUID PRIMARY KEY,
    tenant_key   TEXT UNIQUE NOT NULL,   -- pode ser email, token da instância ou outro identificador estável
    email        TEXT,
    plan         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active','inactive')),
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenants_email ON tenants(email);

CREATE TABLE IF NOT EXISTS payments (
    id            UUID PRIMARY KEY,
    reference_id  TEXT UNIQUE NOT NULL,
    tenant_key    TEXT NOT NULL,
    email         TEXT,
    plan          TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending','paid','failed')),
    raw           JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_email ON payments(email);
CREATE INDEX IF NOT EXISTS idx_payments_tenant_key ON payments(tenant_key);
"""


def init_billing_schema() -> None:
    """Cria tabelas se não existirem."""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
        conn.commit()


# ---------- PAYMENTS ----------
async def create_pending_payment(
    *,
    reference_id: str,
    tenant_key: str,
    email: str,
    plan: str,
    amount_cents: int,
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    """Insere um pagamento em status pending. Se já existe, ignora."""
    import json
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (id, reference_id, tenant_key, email, plan, amount_cents, status, raw)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                ON CONFLICT (reference_id) DO NOTHING
                """,
                (str(uuid.uuid4()), reference_id, tenant_key, email, plan, int(amount_cents), json.dumps(raw or {}))
            )
        conn.commit()


async def update_payment_status(reference_id: str, status: str, raw: Optional[Dict[str, Any]] = None) -> None:
    import json
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payments
                   SET status=%s, raw=COALESCE(%s, raw), updated_at=now()
                 WHERE reference_id=%s
                """,
                (status, json.dumps(raw) if raw else None, reference_id)
            )
        conn.commit()


async def get_payment_by_ref(reference_id: str) -> Optional[Dict[str, Any]]:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE reference_id=%s", (reference_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ---------- TENANTS ----------
async def ensure_tenant_active(
    *,
    tenant_key: str,
    email: Optional[str],
    plan: str,
    months: int = 1,
) -> None:
    """Cria/ativa tenant e estende a validade em N meses (default 1)."""
    pool = get_pool()
    extend_days = 30 * max(1, months)  # simplicidade: 30 dias ~ 1 mês
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tenants WHERE tenant_key=%s", (tenant_key,))
            rec = cur.fetchone()
            if rec:
                # estende a partir do maior entre now e expires_at
                now = datetime.now(timezone.utc)
                expires_at = rec['expires_at'] if isinstance(rec, dict) else rec[4]  # rec[4] é expires_at
                base = expires_at if expires_at and expires_at > now else now
                new_exp = base + timedelta(days=extend_days)
                cur.execute(
                    """
                    UPDATE tenants
                       SET status='active', plan=%s, email=COALESCE(%s, email), expires_at=%s
                     WHERE tenant_key=%s
                    """,
                    (plan, email, new_exp, tenant_key)
                )
            else:
                cur.execute(
                    """
                    INSERT INTO tenants (id, tenant_key, email, plan, status, expires_at)
                    VALUES (%s, %s, %s, %s, 'active', %s)
                    """,
                    (str(uuid.uuid4()), tenant_key, email, plan, datetime.now(timezone.utc) + timedelta(days=extend_days))
                )
        conn.commit()


async def set_tenant_inactive(tenant_key: str) -> None:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tenants SET status='inactive' WHERE tenant_key=%s", (tenant_key,))
        conn.commit()


async def get_tenant(tenant_key: str) -> Optional[Dict[str, Any]]:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tenants WHERE tenant_key=%s", (tenant_key,))
            row = cur.fetchone()
            return dict(row) if row else None


async def is_tenant_active_by_key(tenant_key: str) -> bool:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, expires_at FROM tenants WHERE tenant_key=%s",
                (tenant_key,)
            )
            rec = cur.fetchone()
            if not rec:
                return False
            status = rec['status'] if isinstance(rec, dict) else rec[0]
            expires_at = rec['expires_at'] if isinstance(rec, dict) else rec[1]
            if status != "active":
                return False
            return bool(expires_at and expires_at > datetime.now(timezone.utc))


async def is_tenant_active_by_email(email: str) -> bool:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, expires_at FROM tenants WHERE email=%s",
                (email,)
            )
            rec = cur.fetchone()
            if not rec:
                return False
            status = rec['status'] if isinstance(rec, dict) else rec[0]
            expires_at = rec['expires_at'] if isinstance(rec, dict) else rec[1]
            if status != "active":
                return False
            return bool(expires_at and expires_at > datetime.now(timezone.utc))
