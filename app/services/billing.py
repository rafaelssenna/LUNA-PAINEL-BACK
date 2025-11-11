from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.pg import get_pool  # usa o pool do psycopg_pool

# Número de dias do período de testes gratuito.  Ajustado para 14 dias por padrão.
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS") or 14)
_SALT = (os.getenv("BILLING_SALT") or "luna").encode()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -------------------------------------------------------------------
# CANONICALIZAÇÃO DE CHAVE (sempre por INSTÂNCIA)
# -------------------------------------------------------------------
def canonical_instance_key(key_or_token: str) -> str:
    """
    Normaliza qualquer identificador de instância para o formato:
      iid:<valor>
    - Se já vier com prefixo iid:, mantém.
    - Caso contrário, pré-fixa iid: ao token/id informado.
    """
    s = (key_or_token or "").strip()
    return s if s.startswith("iid:") else f"iid:{s}"


def canonical_email_key(email: str) -> str:
    """
    Cria billing_key baseado em e-mail (identificador principal do usuário).
    Formato: email:<email_lowercase>
    Exemplo: email:usuario@exemplo.com
    """
    e = (email or "").strip().lower()
    if not e:
        raise ValueError("E-mail não pode ser vazio")
    return f"email:{e}"


def canonical_billing_key(billing_key: str) -> str:
    """
    Normaliza qualquer chave de billing (email OU instance_id) para o formato correto.
    
    Entrada:
    - "usuario@exemplo.com" -> "email:usuario@exemplo.com"
    - "email:usuario@exemplo.com" -> "email:usuario@exemplo.com"
    - "inst_123" -> "iid:inst_123"
    - "iid:inst_123" -> "iid:inst_123"
    
    Prioriza email (se contém @) senão trata como instance_id.
    """
    s = (billing_key or "").strip()
    
    # Já está no formato correto
    if s.startswith("email:") or s.startswith("iid:"):
        return s
    
    # É um email (contém @)
    if "@" in s:
        return canonical_email_key(s)
    
    # É instance_id
    return canonical_instance_key(s)


def make_billing_key(token: str, host: str, instance_id: Optional[str]) -> str:
    """
    Preferimos billing por INSTÂNCIA:
      - Se houver instance_id (UUID), usa:   iid:<instance_id>
      - Senão, se houver token, usa:         iid:<token>
      - Fallback legado (raríssimo): hash de host|token (ht:<sha256>)
    """
    if instance_id:
        return canonical_instance_key(instance_id)
    if token:
        return canonical_instance_key(token)

    # Fallback ultra-conservador (evitar None/None)
    raw = f"{host}|{token}".encode()
    digest = hmac.new(_SALT, raw, hashlib.sha256).hexdigest()
    return f"ht:{digest}"


# -------------------------------------------------------------------
# OPERACOES DE TRIAL / STATUS / ATUALIZACOES
# -------------------------------------------------------------------
def ensure_trial(billing_key: str) -> Dict[str, Any]:
    """
    Garante que exista um registro e que o trial esteja iniciado (idempotente).
    Retorna um snapshot básico do registro.
    """
    bkey = canonical_billing_key(billing_key)
    trial_ends = _utcnow() + timedelta(days=TRIAL_DAYS)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # UPSERT idempotente
            cur.execute(
                """
                INSERT INTO billing_accounts
                    (billing_key, created_at, trial_started_at, trial_ends_at)
                VALUES
                    (%s, NOW(), NOW(), %s)
                ON CONFLICT (billing_key) DO NOTHING
                """,
                (bkey, trial_ends),
            )

            # Lê o registro atualizado
            cur.execute(
                """
                SELECT trial_started_at, trial_ends_at, paid_until, plan, last_payment_status
                  FROM billing_accounts
                 WHERE billing_key = %s
                """,
                (bkey,),
            )
            row = cur.fetchone()

    if not row:
        return {
            "trial_started_at": None,
            "trial_ends_at": None,
            "paid_until": None,
            "plan": None,
            "last_payment_status": None,
        }

    def _get(r, k, i):
        try:
            return r[k]
        except Exception:
            return r[i]

    return {
        "trial_started_at": _get(row, "trial_started_at", 0),
        "trial_ends_at": _get(row, "trial_ends_at", 1),
        "paid_until": _get(row, "paid_until", 2),
        "plan": _get(row, "plan", 3),
        "last_payment_status": _get(row, "last_payment_status", 4),
    }


def get_status(billing_key: str) -> Dict[str, Any]:
    """
    Retorna o status atual de billing, incluindo flags de ativo, dias restantes e se requer pagamento.

    REGRAS:
      - Vitalício: last_payment_status='paid' e paid_until IS NULL  -> active=True, plan='vitalicio', days_left=None.
      - Pago com data: paid_until > now().
      - Trial ativo:  trial_ends_at > now().
      - Caso contrário: inactive.
    """
    # ✅ CORRIGIDO: Aceita email OU instance_id
    bkey = canonical_billing_key(billing_key)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trial_started_at, trial_ends_at, paid_until, plan, last_payment_status
                  FROM billing_accounts
                 WHERE billing_key = %s
                """,
                (bkey,),
            )
            row = cur.fetchone()

    if not row:
        return {
            "exists": False,
            "active": False,
            "trial_started_at": None,
            "trial_ends_at": None,
            "paid_until": None,
            "days_left": 0,
            "plan": None,
            "last_payment_status": None,
            "require_payment": False,
        }

    def _get(r, k, i):
        try:
            return r[k]
        except Exception:
            return r[i]

    trial_started = _get(row, "trial_started_at", 0)
    trial_ends    = _get(row, "trial_ends_at", 1)
    paid_until    = _get(row, "paid_until", 2)
    plan          = _get(row, "plan", 3)
    last_status   = (_get(row, "last_payment_status", 4) or "").lower()

    now = _utcnow()

    vitalicio    = (last_status == "paid" and paid_until is None)
    trial_active = bool(trial_ends and trial_ends > now)
    paid_active  = vitalicio or bool(paid_until and paid_until > now)
    active       = bool(trial_active or paid_active)

    if vitalicio:
        days_left = None  # infinito
        plan = plan or "vitalicio"
    elif paid_until and paid_until > now:
        days_left = max(0, (paid_until - now).days)
        plan = plan or "pago"
    elif trial_active:
        days_left = max(0, (trial_ends - now).days)
        plan = plan or "Trial"
    else:
        days_left = 0
        plan = plan or "free"

    return {
        "exists": True,
        "active": active,
        "trial_started_at": trial_started,
        "trial_ends_at": trial_ends,
        "paid_until": paid_until,
        "days_left": days_left,
        "plan": plan,
        "last_payment_status": last_status,
        "require_payment": not active and bool(trial_started),
        "vitalicio": vitalicio,
    }


def mark_paid(
    billing_key: str,
    days: int = 30,
    plan: Optional[str] = None,
    status: str = "paid",
) -> None:
    """
    Avança/define paid_until por N dias a partir do maior entre agora e o paid_until atual.
    Atualiza também plan e last_payment_status.
    """
    # ✅ CORRIGIDO: Aceita email OU instance_id
    bkey = canonical_billing_key(billing_key)
    now = _utcnow()
    add_days = max(1, int(days))

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # garante existência do registro (idempotente)
            cur.execute(
                """
                INSERT INTO billing_accounts (billing_key, created_at, trial_started_at, trial_ends_at)
                VALUES (%s, NOW(), NOW(), %s)
                ON CONFLICT (billing_key) DO NOTHING
                """,
                (bkey, now + timedelta(days=TRIAL_DAYS)),
            )

            cur.execute(
                "SELECT paid_until FROM billing_accounts WHERE billing_key = %s",
                (bkey,),
            )
            row = cur.fetchone()
            paid_atual = None
            if row is not None:
                try:
                    paid_atual = row["paid_until"]
                except Exception:
                    paid_atual = row[0]

            base = paid_atual if (paid_atual and paid_atual > now) else now
            new_paid = base + timedelta(days=add_days)

            cur.execute(
                """
                UPDATE billing_accounts
                   SET paid_until = %s,
                       plan = COALESCE(%s, plan),
                       last_payment_status = %s
                 WHERE billing_key = %s
                """,
                (new_paid, plan, status, bkey),
            )


def mark_status(
    billing_key: str,
    status: str,
    plan: Optional[str] = None,
) -> None:
    """
    Atualiza apenas o last_payment_status (e opcionalmente o plan), sem mexer no paid_until.
    Útil para marcar falhas (status='failed') ou outros estados temporários.
    """
    # ✅ CORRIGIDO: Aceita email OU instance_id
    bkey = canonical_billing_key(billing_key)

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # garante existência do registro (idempotente)
            cur.execute(
                """
                INSERT INTO billing_accounts (billing_key, created_at, trial_started_at, trial_ends_at)
                VALUES (%s, NOW(), NOW(), %s)
                ON CONFLICT (billing_key) DO NOTHING
                """,
                (bkey, _utcnow() + timedelta(days=TRIAL_DAYS)),
            )
            cur.execute(
                """
                UPDATE billing_accounts
                   SET last_payment_status = %s,
                       plan = COALESCE(%s, plan)
                 WHERE billing_key = %s
                """,
                (status, plan, bkey),
            )


async def is_billing_active(billing_key: str) -> bool:
    """
    Verifica se o billing está ativo (trial ou pagamento).
    Retorna True se:
      - Trial ainda está ativo (trial_ends_at > now), OU
      - Pagamento ativo (paid_until > now), OU
      - Vitalício (last_payment_status='paid' e paid_until IS NULL)
    
    Args:
        billing_key: Chave de billing (formato: email:user@example.com ou iid:instance_id)
    
    Returns:
        bool: True se billing está ativo, False caso contrário
    """
    try:
        status = get_status(billing_key)
        return status.get("active", False)
    except Exception:
        # Em caso de erro, retorna False para não bloquear
        return False
