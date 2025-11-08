# app/routes/pay_stripe.py
"""
Stripe payment integration routes for Luna.

Flow (resumo):
1) Criar sessão via /checkout (ou /checkout-url) e redirecionar para `session.url`.
2) Stripe chama /webhook com eventos (ex.: invoice.paid, invoice.payment_failed).
3) No paid: marcamos o pagamento como "paid" e deixamos o tenant ativo por 1 mês.
4) No failed/cancel: atualizamos o pagamento para "failed" e desativamos quando necessário.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import stripe  # type: ignore
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.models_billing import (
    create_pending_payment,
    update_payment_status,
    ensure_tenant_active,
    set_tenant_inactive,
)
# >>> NOVO: persistir período pago/estado no billing_accounts (refletir no /billing/status)
from app.services.billing import mark_paid, mark_status  # mark_status cobre "failed" etc.

router = APIRouter()

# -----------------------------------------------------------------------------
# Stripe configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

stripe.api_key = STRIPE_SECRET_KEY or None

PUBLIC_BASE = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
# Retornos apontam para a RAIZ do site (páginas estáticas /pagamentos foram removidas).
RETURN_BASE = (os.getenv("STRIPE_RETURN_BASE") or f"{PUBLIC_BASE}/").rstrip("/")
CANCEL_BASE = (os.getenv("STRIPE_CANCEL_BASE") or f"{PUBLIC_BASE}/").rstrip("/")
# Apenas referência para configuração no dashboard (não usado diretamente abaixo).
NOTIFY_URL = (os.getenv("STRIPE_NOTIFY_URL") or f"{PUBLIC_BASE}/api/pay/stripe/webhook").rstrip("/")

DEFAULT_PRICE_CENTS = int(os.getenv("LUNA_PRICE_CENTS") or 34990)


# ---------------------------- Request/Response models -------------------------
class CheckoutIn(BaseModel):
    """
    Input payload for creating a checkout session.
    """
    email: EmailStr
    plan: str = Field(default="luna_base")
    amount_cents: int = Field(default=DEFAULT_PRICE_CENTS)
    tenant_key: Optional[str] = Field(default=None)


class CheckoutOut(BaseModel):
    """
    Response for checkout creation.
    """
    ref: str
    url: str


# ---------------------------- Helper functions -------------------------------
def _build_success_url(ref: str) -> str:
    base = RETURN_BASE or ""
    return f"{base}?success=true" if base else "?success=true"


def _build_cancel_url(ref: str) -> str:
    base = CANCEL_BASE or ""
    return f"{base}?canceled=true" if base else "?canceled=true"


def _extract_event_parts(event: Any) -> Tuple[str, Dict[str, Any]]:
    """
    Extrai (event_type, data_object) de um dict ou stripe.Event.
    Garante que sempre retornaremos estruturas nativas (dict).
    """
    # Caso 1: payload já em dict (sem validação de assinatura)
    if isinstance(event, dict):
        etype = event.get("type", "")
        data_obj = (event.get("data") or {}).get("object") or {}
        return etype, data_obj if isinstance(data_obj, dict) else {}

    # Caso 2: stripe.Event / StripeObject
    try:
        etype = getattr(event, "type", "") or event["type"]
    except Exception:
        etype = ""

    try:
        data = getattr(event, "data", None) or event["data"]
        obj = getattr(data, "object", None) or data["object"]
        # StripeObject -> dict
        if hasattr(obj, "to_dict"):
            obj = obj.to_dict()  # type: ignore
        return etype, obj if isinstance(obj, dict) else {}
    except Exception:
        return etype, {}

# ---------------------------- Checkout endpoints -----------------------------
@router.post("/checkout", response_model=CheckoutOut)
async def create_checkout(body: CheckoutIn) -> CheckoutOut:
    """
    Cria uma sessão de Stripe Checkout (modo assinatura) e retorna a URL.
    """
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe não configurado.")

    # Referência interna
    ref = f"st_{uuid.uuid4().hex}"

    # ⚠️ AGORA É OBRIGATÓRIO: uso EXCLUSIVO do token da instância
    tenant_key = (body.tenant_key or "").strip()
    if not tenant_key:
        raise HTTPException(status_code=400, detail="Token da instância (tenant_key) é obrigatório.")

    # 1) cria/atualiza registro local "pending" (não bloqueia o fluxo)
    try:
        await create_pending_payment(
            reference_id=ref,
            tenant_key=tenant_key,
            email=str(body.email),
            plan=body.plan,
            amount_cents=int(body.amount_cents),
            raw=None,
        )
    except Exception:
        # Em caso de falha local, seguimos; o webhook poderá reconciliar.
        pass

    # 2) gera sessão de checkout no Stripe
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=_build_success_url(ref),
            cancel_url=_build_cancel_url(ref),
            client_reference_id=ref,
            customer_email=str(body.email),
            # Metadata na sessão e na assinatura para correlacionar no webhook
            metadata={
                "reference_id": ref,
                "tenant_key": tenant_key,
                "plan": body.plan,
                "email": str(body.email),
            },
            subscription_data={
                "metadata": {
                    "reference_id": ref,
                    "tenant_key": tenant_key,
                    "plan": body.plan,
                    "email": str(body.email),
                }
            },
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Falha ao criar sessão de pagamento: {e}")

    # 3) persiste "pending" com o session_id
    try:
        await update_payment_status(ref, "pending", raw={"session_id": session.id})
    except Exception:
        pass

    return CheckoutOut(ref=ref, url=session.url)


@router.get("/checkout-url", response_model=CheckoutOut)
async def get_checkout_url(
    email: Optional[EmailStr] = None,
    plan: str = "luna_base",
    amount_cents: int = DEFAULT_PRICE_CENTS,
    tenant_key: Optional[str] = None,
) -> CheckoutOut:
    """
    Versão GET para facilitar redirecionamento a partir do front.
    Se `email` não for enviado, criamos um anônimo (apenas para teste).
    """
    import uuid as _uuid
    actual_email: EmailStr = email or EmailStr(f"anon-{_uuid.uuid4().hex}@example.com")  # type: ignore
    body = CheckoutIn(
        email=actual_email,
        plan=plan,
        amount_cents=amount_cents,
        tenant_key=tenant_key,
    )
    return await create_checkout(body)


# ---------------------------- Webhook handler -------------------------------
@router.post("/webhook")
async def stripe_webhook(request: Request) -> Dict[str, Any]:
    """
    Webhook da Stripe.

    Este endpoint processa eventos enviados pelo Stripe e sincroniza o status de
    pagamentos/assinaturas com o banco de dados interno.  A lógica abaixo cobre
    tanto pagamentos de faturas (``invoice.paid``) quanto a conclusão do
    checkout de assinatura (``checkout.session.completed``) e falhas ou
    cancelamentos.

    * ``invoice.paid``: marca o pagamento como "paid", ativa o tenant por 1 mês
      e estende o período de pagamento no ``billing_accounts`` por 30 dias.
    * ``invoice.payment_failed``: marca o pagamento como "failed" e atualiza o
      ``billing_accounts`` com ``last_payment_status='failed'``.
    * ``customer.subscription.deleted``: marca "failed", atualiza
      ``billing_accounts`` e desativa o tenant.
    * ``checkout.session.completed``: quando o usuário finaliza o checkout em
      modo assinatura, alguns fluxos (especialmente pagamentos fora do fluxo de
      fatura) não disparam imediatamente ``invoice.paid``.  Para garantir que
      o sistema reconheça o pagamento logo após o checkout, processamos este
      evento como sinônimo de sucesso, extraindo a metadata e executando as
      mesmas ações de um ``invoice.paid``.
    """

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    event: Any

    # Com secret -> valida assinatura; sem secret (homolog/dev) -> parse JSON simples
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Webhook signature verification failed: {e}")
    else:
        try:
            event = json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    event_type, data_object = _extract_event_parts(event)

    async def _process_success(meta: Dict[str, Any], raw_obj: Dict[str, Any], etype: str) -> None:
        """Processa o sucesso de pagamento ou checkout.

        Esta função marca o pagamento como "paid" (se houver uma referência),
        ativa o tenant por 1 mês e atualiza o ``billing_accounts`` com os dias
        pagos.  Todas as exceções são capturadas para evitar que o webhook
        retorne erro ao Stripe.
        """
        ref = meta.get("reference_id")
        tenant_key = meta.get("tenant_key") or None
        plan = meta.get("plan", "luna_base")
        email = meta.get("email")

        # Atualiza o status do pagamento (tabela payments)
        if ref:
            try:
                await update_payment_status(ref, "paid", raw={"type": etype, "object": raw_obj})
            except Exception:
                pass

        # Ativa o tenant (tabela tenants) por 1 mês
        if tenant_key:
            try:
                await ensure_tenant_active(
                    tenant_key=tenant_key or ref or "",
                    email=email,
                    plan=plan,
                    months=1,
                )
            except Exception:
                pass

            # Atualiza billing_accounts (tabela interna de billing) com o novo período
            try:
                mark_paid(billing_key=str(tenant_key), days=30, plan=plan, status="paid")
            except Exception:
                pass

    # -------------------- invoice.paid --------------------
    if event_type == "invoice.paid":
        subscription_id = data_object.get("subscription")
        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                meta: Dict[str, Any] = getattr(sub, "metadata", None) or {}
                # StripeObject → dict
                if hasattr(meta, "to_dict"):
                    meta = meta.to_dict()  # type: ignore
                await _process_success(meta, data_object, event_type)
            except Exception:
                # Não quebra o webhook; Stripe só precisa de 2xx
                pass

    # -------------------- invoice.payment_failed --------------------
    elif event_type == "invoice.payment_failed":
        subscription_id = data_object.get("subscription")
        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                meta: Dict[str, Any] = getattr(sub, "metadata", None) or {}
                if hasattr(meta, "to_dict"):
                    meta = meta.to_dict()  # type: ignore
                ref = meta.get("reference_id")
                tenant_key = meta.get("tenant_key")
                plan = meta.get("plan")
                # Atualiza payment para failed
                if ref:
                    try:
                        await update_payment_status(ref, "failed", raw={"type": event_type, "object": data_object})
                    except Exception:
                        pass
                # Marca status de falha no billing_accounts
                if tenant_key:
                    try:
                        mark_status(billing_key=str(tenant_key), status="failed", plan=plan)
                    except Exception:
                        pass
            except Exception:
                pass

    # -------------------- customer.subscription.deleted --------------------
    elif event_type == "customer.subscription.deleted":
        # Para cancelamentos e remoções de assinatura, a metadata fica
        # diretamente em data_object.metadata
        meta: Dict[str, Any] = data_object.get("metadata", {}) or {}
        ref = meta.get("reference_id")
        tenant_key = meta.get("tenant_key")
        plan = meta.get("plan")
        # Atualiza payment para failed
        if ref:
            try:
                await update_payment_status(ref, "failed", raw={"type": event_type, "object": data_object})
            except Exception:
                pass
        # Marca status de falha no billing_accounts
        if tenant_key:
            try:
                mark_status(billing_key=str(tenant_key), status="failed", plan=plan)
            except Exception:
                pass
            # Desativa o tenant operacional
            try:
                await set_tenant_inactive(tenant_key)
            except Exception:
                pass

    # -------------------- checkout.session.completed --------------------
    elif event_type == "checkout.session.completed":
        # Sessão concluída: usa metadata da sessão ou busca a assinatura
        meta: Dict[str, Any] = data_object.get("metadata", {}) or {}
        # Alguns eventos incluem subscription no objeto
        subscription_id = data_object.get("subscription") or data_object.get("subscription_id")
        # Se a metadata estiver vazia e houver subscription, tenta extrair da assinatura
        if (not meta or not meta.get("reference_id")) and subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
                meta_sub: Dict[str, Any] = getattr(sub, "metadata", None) or {}
                if hasattr(meta_sub, "to_dict"):
                    meta_sub = meta_sub.to_dict()  # type: ignore
                # Prioriza metadata da assinatura caso exista
                meta = {**meta, **meta_sub}
            except Exception:
                pass
        # Se ainda não houver reference_id, tenta usar client_reference_id
        if not meta.get("reference_id"):
            ref = data_object.get("client_reference_id")
            if ref:
                meta["reference_id"] = ref
        # Processa como sucesso
        await _process_success(meta, data_object, event_type)

    # Retorna sucesso genérico (Stripe precisa apenas de 2xx)
    return {"ok": True}
