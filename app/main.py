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
    instances,  # ✅ gerenciamento de instâncias WhatsApp
    pay_stripe,  # ✅ rotas de pagamento (Stripe)
    questionnaires,  # ✅ questionários de onboarding
)

# Auth da instância (UAZAPI): monta /api/auth corretamente a partir de app/auth.py
from .auth import router as auth_router  # login via token da instância

# Schema inicial (seu módulo existente)
from .pg import init_schema  # mantém como está, caso já crie outros schemas

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

# Se não houver origens configuradas OU estiver em localhost, permitir todas
if not _all_origins or os.getenv("ALLOW_ALL_ORIGINS", "0") == "1":
    _all_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_all_origins,
    allow_origin_regex=allowed_origin_regex(),
    allow_credentials=True if "*" not in _all_origins else False,
    allow_methods=["*"],          # ⬅ preflight liberado
    allow_headers=["*"],          # ⬅ preflight liberado
    expose_headers=["*"],
    max_age=600,
)


# --------------------------- Startup ----------------------------------- #
@app.on_event("startup")
async def _startup():
    import asyncio
    logger = logging.getLogger("uvicorn.error")
    logger.info("Inicializando Luna Backend.")
    logger.info("CORS allow_origins: %s", _all_origins)
    logger.info("CORS allow_origin_regex: %s", allowed_origin_regex())

    # Validar variáveis críticas
    db_url = os.getenv("DATABASE_URL") or ""
    if not db_url:
        logger.error("❌ DATABASE_URL não definido! Defina a variável de ambiente.")
    else:
        safe_db = db_url.split("@")[-1]
        logger.info("✅ DATABASE_URL detectado (host/db: %s)", safe_db)

    # Validar UAZAPI (WhatsApp)
    uazapi_admin_token = os.getenv("UAZAPI_ADMIN_TOKEN") or ""
    if not uazapi_admin_token or uazapi_admin_token == "PRECISA_FORNECER_ESSE_TOKEN":
        logger.error("❌ UAZAPI_ADMIN_TOKEN não configurado!")
        logger.error("❌ Sem esse token, não será possível criar instâncias WhatsApp.")
        logger.error("❌ Configure no .env: UAZAPI_ADMIN_TOKEN=seu_token_aqui")
    else:
        logger.info("✅ UAZAPI_ADMIN_TOKEN configurado (length: %d)", len(uazapi_admin_token))

    # Validar OpenAI (IA)
    openai_key = os.getenv("OPENAI_API_KEY") or ""
    if not openai_key or openai_key == "PRECISA_FORNECER":
        logger.warning("⚠️ OPENAI_API_KEY não configurado!")
        logger.warning("⚠️ A IA não funcionará sem essa chave.")
        logger.warning("⚠️ Configure no .env: OPENAI_API_KEY=sk-...")
    else:
        logger.info("✅ OPENAI_API_KEY configurado")

    try:
        # Seu schema padrão (lead_status, users etc.)
        init_schema()
        logger.info("Schemas verificados/criados com sucesso (lead_status/users/afins).")
    except Exception:
        logger.exception("Falha ao inicializar schema do banco (módulo .pg).")

    # Iniciar task de limpeza de memory leaks
    try:
        from .routes.webhook import cleanup_stale_buffers
        asyncio.create_task(cleanup_stale_buffers())
        logger.info("✅ Task de limpeza de buffers iniciada")
    except Exception as e:
        logger.error(f"❌ Erro ao iniciar task de limpeza: {e}")

    # DESABILITADO: Não usamos tabelas tenants/payments, usamos billing
    # try:
    #     await init_billing_schema()
    #     logger.info("Billing schema verificado/criado com sucesso (tenants/payments).")
    # except Exception:
    #     logger.exception("Falha ao inicializar billing schema.")

# ---------------------------- Rotas ------------------------------------ #
# Auth de instância (UAZAPI)
app.include_router(auth_router,        prefix="/api/auth",    tags=["auth"])

# Auth de usuário (e-mail/senha)
app.include_router(users.router,       prefix="/api/users",   tags=["users"])

# Gerenciamento de instâncias WhatsApp
app.include_router(instances.router,   prefix="/api/instances", tags=["instances"])

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
app.include_router(pay_stripe.router,  prefix="/api/pay/stripe", tags=["stripe"])
app.include_router(questionnaires.router, prefix="/api/questionnaires", tags=["questionnaires"])

# Healthcheck simples
@app.get("/healthz")
async def healthz():
    return {"ok": True}

# Catch‑all para preflight (reforço ao CORSMiddleware)
@app.options("/{rest_of_path:path}", include_in_schema=False)
async def _cors_preflight_catchall(rest_of_path: str):
    return Response(status_code=204)
