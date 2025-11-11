# app/routes/billing.py
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.services.billing import (
    make_billing_key,
    ensure_trial,
    get_status,
    canonical_instance_key,  # <<< USADO AQUI
)
# A integração legada GetNet foi removida; cobrança agora ocorre apenas via Stripe.

router = APIRouter()


# ------------------------ helpers ------------------------
def _env_list(name: str) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _is_admin_bypass(user: Dict[str, Any]) -> bool:
    """
    Bypass para contas administradoras. Ativa caso QUALQUER uma das listas case:
      - ADMIN_BYPASS_EMAILS  (com base em user['email'] ou user['user_email'])
      - ADMIN_BYPASS_HOSTS   (com base em user['host'])
      - ADMIN_BYPASS_TOKENS  (com base em user['token'] ou user['instance_token'])
    """
    emails = set(x.lower() for x in _env_list("ADMIN_BYPASS_EMAILS"))
    hosts = set(_env_list("ADMIN_BYPASS_HOSTS"))
    toks = set(_env_list("ADMIN_BYPASS_TOKENS"))

    email = (user.get("email") or user.get("user_email") or "").lower().strip()
    host = (user.get("host") or "").strip()
    token = (user.get("token") or user.get("instance_token") or "").strip()

    if email and email in emails:
        return True
    if host and host in hosts:
        return True
    if token and token in toks:
        return True
    return False


def _billing_key_from_user(user: Dict[str, Any]) -> str:
    """
    Monta um ``billing_key`` a partir do JWT do usuário.

    NOVA PRIORIDADE (baseada em e-mail):
      1) E-MAIL: identificador principal (email:<email_lowercase>)
      2) Fallback legado: token/instance_id para compatibilidade

    O sistema é billing por e-mail: cada e-mail pode ter UMA instância.
    """
    # 1) PRIORIDADE: E-mail (identificador principal)
    email = (user.get("email") or user.get("user_email") or "").strip().lower()
    if email:
        from app.services.billing import canonical_email_key
        return canonical_email_key(email)

    # 2) Fallback: JWT de usuário com sub='user:<id>'
    sub = str(user.get("sub") or "")
    if sub.startswith("user:"):
        uid = sub.split(":", 1)[1]
        if uid:
            return f"uid:{uid}"

    # 3) Fallback legado: instância (para compatibilidade)
    token = (user.get("token") or user.get("instance_token") or "").strip()
    iid = (user.get("instance_id") or "") or ""
    if iid or token:
        return canonical_instance_key(iid or token)

    # Sem dados suficientes.
    raise HTTPException(status_code=401, detail="JWT inválido: sem email/token/sub")


def _safe_get_status(bkey: str) -> Dict[str, Any]:
    """Lê o status do billing sem deixar a rota explodir em 500."""
    try:
        return get_status(bkey)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Billing indisponível: {e}")


# ------------------------ modelos ------------------------
class CheckoutLinkIn(BaseModel):
    return_url: Optional[str] = None  # (sem uso direto aqui; mantido para compat.)


class WebhookIn(BaseModel):
    ref: str                         # billing_key enviado como reference/order_id
    status: str = "paid"             # 'paid', 'approved', 'refused', etc.
    days: int = 30                   # dias para estender o paid_until
    plan: Optional[str] = "mensal-34990"


# ------------------------ rotas ------------------------
@router.post("/register-trial")
async def register_trial(user=Depends(get_current_user)) -> Dict[str, Any]:
    """
    Garante o registro do tenant e inicia o trial se não existir.
    Admin (bypass): sempre ativo sem tocar no billing.
    """
    if _is_admin_bypass(user):
        return {
            "ok": True,
            "billing_key": None,
            "status": {"active": True, "plan": "admin", "admin_bypass": True},
        }

    bkey = _billing_key_from_user(user)

    # tenta ler status; se não existir, cria trial
    try:
        st = get_status(bkey)
    except Exception:
        st = {"exists": False, "active": False}

    if not st.get("exists"):
        try:
            ensure_trial(bkey)
        except Exception:
            # mantém idempotência mesmo se o serviço falhar; status é lido abaixo
            pass

    st = _safe_get_status(bkey)
    return {"ok": True, "billing_key": bkey, "status": st}


@router.get("/status")
async def billing_status(user=Depends(get_current_user)) -> Dict[str, Any]:
    """
    Retorna o status atual do billing (active, days_left, trial_ends_at, paid_until, plan...).
    Admin (bypass): sempre ativo.
    """
    if _is_admin_bypass(user):
        return {
            "ok": True,
            "billing_key": None,
            "status": {"active": True, "plan": "admin", "admin_bypass": True},
        }

    bkey = _billing_key_from_user(user)
    st = _safe_get_status(bkey)
    return {"ok": True, "billing_key": bkey, "status": st}

# Observação: cobranca via Stripe → ver routes/pay_stripe.py
