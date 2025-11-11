# app/db/models_billing.py
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

import asyncpg
from datetime import datetime, timedelta, timezone

DATABASE_URL = os.getenv("DATABASE_URL") or ""

_POOL: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL não definido")
        _POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
    return _POOL


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


async def init_billing_schema() -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)


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
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO payments (id, reference_id, tenant_key, email, plan, amount_cents, status, raw)
            VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)
            ON CONFLICT (reference_id) DO NOTHING
            """,
            uuid.uuid4(),
            reference_id,
            tenant_key,
            email,
            plan,
            int(amount_cents),
            raw or {},
        )


async def update_payment_status(reference_id: str, status: str, raw: Optional[Dict[str, Any]] = None) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            UPDATE payments
               SET status=$2, raw=COALESCE($3, raw), updated_at=now()
             WHERE reference_id=$1
            """,
            reference_id,
            status,
            raw or None,
        )


async def get_payment_by_ref(reference_id: str) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM payments WHERE reference_id=$1", reference_id)


# ---------- TENANTS ----------
async def ensure_tenant_active(
    *,
    tenant_key: str,
    email: Optional[str],
    plan: str,
    months: int = 1,
) -> None:
    """Cria/ativa tenant e estende a validade em N meses (default 1)."""
    pool = await get_pool()
    extend_days = 30 * max(1, months)  # simplicidade: 30 dias ~ 1 mês
    async with pool.acquire() as con:
        rec = await con.fetchrow("SELECT * FROM tenants WHERE tenant_key=$1", tenant_key)
        if rec:
            # estende a partir do maior entre now e expires_at
            now = datetime.now(timezone.utc)
            base = rec["expires_at"] if rec["expires_at"] and rec["expires_at"] > now else now
            new_exp = base + timedelta(days=extend_days)
            await con.execute(
                """
                UPDATE tenants
                   SET status='active', plan=$2, email=COALESCE($3, email), expires_at=$4
                 WHERE tenant_key=$1
                """,
                tenant_key,
                plan,
                email,
                new_exp,
            )
        else:
            await con.execute(
                """
                INSERT INTO tenants (id, tenant_key, email, plan, status, expires_at)
                VALUES ($1, $2, $3, $4, 'active', $5)
                """,
                uuid.uuid4(),
                tenant_key,
                email,
                plan,
                datetime.now(timezone.utc) + timedelta(days=extend_days),
            )


async def set_tenant_inactive(tenant_key: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE tenants SET status='inactive' WHERE tenant_key=$1", tenant_key)


async def get_tenant(tenant_key: str) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM tenants WHERE tenant_key=$1", tenant_key)


async def is_tenant_active_by_key(tenant_key: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        rec = await con.fetchrow(
            "SELECT status, expires_at FROM tenants WHERE tenant_key=$1",
            tenant_key,
        )
        if not rec:
            return False
        if rec["status"] != "active":
            return False
        exp = rec["expires_at"]
        return bool(exp and exp > datetime.now(timezone.utc))


async def is_tenant_active_by_email(email: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        rec = await con.fetchrow(
            "SELECT status, expires_at FROM tenants WHERE email=$1",
            email,
        )
        if not rec:
            return False
        if rec["status"] != "active":
            return False
        exp = rec["expires_at"]
        return bool(exp and exp > datetime.now(timezone.utc))
