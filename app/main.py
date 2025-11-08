from __future__ import annotations

import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

# Carregar .env ANTES de tudo
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Rotas internas
from .routes import (
    chats,
    messages,
    send,
    realtime,
    meta,
    name_image,
    crm,
    media,
    ai,
    lead_status,
    billing,
    users,
    admin,  # ✅ rotas administrativas
    webhook,  # ✅ agente IA
)
from .routes import pay_stripe  # ✅ rotas de pagamento (Stripe)

# Auth da instância (UAZAPI): monta /api/auth corretamente a partir de app/auth.py
from .auth import router as auth_router  # login via token da instância

# Schema inicial (seu módulo existente)
from .pg import init_schema  # mantém como está, caso já crie outros schemas

# 🔧 Billing schema (novo): garante que 'tenants' e 'payments' existam
from .models_billing import init_billing_schema

def allowed_origins() -> list[str]:
    allowlist = set()
    # FRONTEND_ORIGINS: lista separada por vírgula (ex.: "https://a.com,https://b.com")
    front_env = os.getenv("FRONTEND_ORIGINS", "")
    if front_env:
        for item in front_env.split(","):
            item = item.strip()
            if item:
                allowlist.add(item)
    # Allow localhost para testes
    if os.getenv("ALLOW_LOCALHOST", "1") == "1":
        allowlist.update(
            {
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            }
        )
    return sorted(allowlist)

def allowed_origin_regex() -> str | None:
    rx = (os.getenv("FRONTEND_ORIGIN_REGEX") or "").strip()
    return rx or None

app = FastAPI(title="Luna Backend", version="1.0.0")

# CORS — aceita lista e/ou regex
_default_origins = {
    "https://www.lunahia.com.br",
    "https://lunahia.com.br",  # opcional, sem www
}
_env_origins = set(allowed_origins())
_all_origins = sorted(_default_origins.union(_env_origins))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_all_origins,
    allow_origin_regex=allowed_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],          # ⬅ preflight liberado
    allow_headers=["*"],          # ⬅ preflight liberado
    max_age=600,
)


# --------------------------- Startup ----------------------------------- #
@app.on_event("startup")
async def _startup():
    logger = logging.getLogger("uvicorn.error")
    logger.info("Inicializando Luna Backend.")
    logger.info("CORS allow_origins: %s", _all_origins)
    logger.info("CORS allow_origin_regex: %s", allowed_origin_regex())

    db_url = os.getenv("DATABASE_URL") or ""
    if not db_url:
        logger.error("DATABASE_URL não definido! Defina a variável de ambiente.")
    else:
        safe_db = db_url.split("@")[-1]
        logger.info("DATABASE_URL detectado (host/db: %s)", safe_db)

    try:
        # Seu schema padrão (lead_status, users etc.)
        init_schema()
        logger.info("Schemas verificados/criados com sucesso (lead_status/users/afins).")
    except Exception:
        logger.exception("Falha ao inicializar schema do banco (módulo .pg).")

    try:
        # 🔧 Garante também o schema de billing (tenants/payments)
        await init_billing_schema()
        logger.info("Billing schema verificado/criado com sucesso (tenants/payments).")
    except Exception:
        logger.exception("Falha ao inicializar billing schema.")

# ---------------------------- Rotas ------------------------------------ #
# Auth de instância (UAZAPI)
app.include_router(auth_router,        prefix="/api/auth",    tags=["auth"])

# Auth de usuário (e-mail/senha)
app.include_router(users.router,       prefix="/api/users",   tags=["users"])

# Admin (painel administrativo)
app.include_router(admin.router,       prefix="/api/admin",   tags=["admin"])

# Webhook WhatsApp (agente IA)
app.include_router(webhook.router,     prefix="/api",         tags=["webhook"])

# Core
app.include_router(chats.router,       prefix="/api",         tags=["chats"])
app.include_router(messages.router,    prefix="/api",         tags=["messages"])
app.include_router(send.router,        prefix="/api",         tags=["send"])
app.include_router(realtime.router,    prefix="/api",         tags=["realtime"])
app.include_router(meta.router,        prefix="/api",         tags=["meta"])
app.include_router(name_image.router,  prefix="/api",         tags=["name-image"])
app.include_router(crm.router,         prefix="/api",         tags=["crm"])
app.include_router(media.router,       prefix="/api/media",   tags=["media"])
app.include_router(lead_status.router, prefix="/api",         tags=["lead-status"])
app.include_router(billing.router,     prefix="/api/billing", tags=["billing"])

# Pagamentos (Stripe: checkout + webhook)
app.include_router(pay_stripe.router,  prefix="/api/pay/stripe", tags=["stripe"])

# Healthcheck simples
@app.get("/healthz")
async def healthz():
    return {"ok": True}

# Catch‑all para preflight (reforço ao CORSMiddleware)
@app.options("/{rest_of_path:path}", include_in_schema=False)
async def _cors_preflight_catchall(rest_of_path: str):
    return Response(status_code=204)
