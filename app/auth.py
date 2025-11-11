from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import jwt  # PyJWT
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

router = APIRouter()
security = HTTPBearer(auto_error=True)

# --------- ENV helpers ---------
def _get_env_str(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _get_exp_minutes() -> int:
    """
    Prioridades:
      1) JWT_EXPIRE_MINUTES (minutos)
      2) LUNA_JWT_TTL (segundos)
      3) fallback: 30 dias
    """
    vmin = os.getenv("JWT_EXPIRE_MINUTES")
    if vmin and vmin.isdigit():
        return int(vmin)
    vttl = os.getenv("LUNA_JWT_TTL")
    if vttl and vttl.isdigit():
        secs = int(vttl)
        return max(1, secs // 60)
    return 43200  # 30 dias

JWT_SECRET          = _get_env_str("JWT_SECRET", "LUNA_JWT_SECRET", default="change-me")
JWT_ALGORITHM       = _get_env_str("JWT_ALGORITHM", default="HS256")
JWT_EXPIRE_MINUTES  = _get_exp_minutes()
DEFAULT_UAZAPI_HOST = _get_env_str("UAZAPI_HOST")  # ex: api.uazapi.com

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# --------- MODELOS ---------
class LoginIn(BaseModel):
    token: str                         # token/sessão da instância na UAZAPI (ou UUID)
    host: Optional[str] = None         # se não vier, usa UAZAPI_HOST
    label: Optional[str] = None
    number_hint: Optional[str] = None

class LoginOut(BaseModel):
    jwt: str
    profile: Dict[str, Any]

# --------- JWT helpers ---------
def _jwt_encode(payload: dict) -> str:
    try:
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao assinar JWT: {e}")

def _jwt_decode(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="JWT expirado")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"JWT inválido/expirado: {e}")

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    tok = credentials.credentials or ""
    if not tok:
        raise HTTPException(status_code=401, detail="Sem credenciais")
    return _jwt_decode(tok)

# --------- ROTAS ---------
@router.post("/login", response_model=LoginOut)
def login(body: LoginIn) -> LoginOut:
    """
    Gera um JWT contendo:
      - token: token/sessão da UAZAPI (obrigatório)
      - host: host/base da UAZAPI (obrigatório; vem do body ou de UAZAPI_HOST)
      - instance_id (quando o 'token' tiver cara de UUID)
      - claims auxiliares (label/number_hint) para a UI
    
    VALIDAÇÃO: Impede conectar o mesmo número de telefone em múltiplas instâncias
    """
    raw_token = (body.token or "").strip()
    if not raw_token:
        raise HTTPException(status_code=400, detail="Informe o token da instância")

    host = (body.host or DEFAULT_UAZAPI_HOST or "").strip()
    if not host:
        raise HTTPException(
            status_code=400,
            detail="Host da UAZAPI ausente. Defina a env UAZAPI_HOST ou envie 'host' no login.",
        )

    # normaliza host (sem protocolo / barras finais)
    host = host.replace("https://", "").replace("http://", "").strip("/")
    
    # VALIDAÇÃO: Verificar se este token já está em uso
    from app.pg import get_pool
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Verificar se este token já está cadastrado
                cur.execute(
                    "SELECT id, phone_number, status FROM instances WHERE uazapi_token = %s",
                    (raw_token,)
                )
                existing = cur.fetchone()
                
                if existing and existing['phone_number']:
                    # Token já existe e tem número conectado
                    phone = existing['phone_number']
                    status = existing['status']
                    
                    # Se está conectado, está em uso
                    if status == 'connected':
                        raise HTTPException(
                            status_code=400,
                            detail=f"Este número ({phone}) já está conectado em outra instância. Não é possível usar o mesmo número em múltiplas instâncias."
                        )
    except HTTPException:
        raise
    except Exception as e:
        # Se falhar a validação, permite continuar (não bloqueia login)
        import logging
        logging.getLogger("uvicorn.error").warning(f"[AUTH] Falha ao validar duplicação: {e}")

    # Se parecer um UUID, guardamos como instance_id
    instance_id: Optional[str] = raw_token if UUID_RE.match(raw_token) else None

    now = datetime.utcnow()
    exp = now + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "iss": "luna-backend",
        "sub": "luna-user",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        # usados pelas rotas/deps:
        "token": raw_token,
        "host": host,
        # compat/extra:
        "instance_token": raw_token,
        "instance_id": instance_id,
        "label": (body.label or None),
        "number_hint": (body.number_hint or None),
    }

    tok = _jwt_encode(payload)

    return LoginOut(
        jwt=tok,
        profile={
            "label": payload.get("label"),
            "number_hint": payload.get("number_hint"),
            "host": host,
            "has_instance_id": bool(instance_id),
        },
    )

@router.get("/check")
def check(user=Depends(get_current_user)):
    return {
        "ok": True,
        "user": {
            "host": user.get("host"),
            "has_instance_id": bool(user.get("instance_id")),
            "label": user.get("label"),
            "number_hint": user.get("number_hint"),
        },
    }

@router.get("/me")
def me(user=Depends(get_current_user)):
    return user

@router.get("/debug")
def debug():
    tail = JWT_SECRET[-5:] if len(JWT_SECRET) >= 5 else "***"
    return {
        "alg": JWT_ALGORITHM,
        "exp_minutes": JWT_EXPIRE_MINUTES,
        "secret_len": len(JWT_SECRET),
        "secret_tail": tail,
        "default_host": DEFAULT_UAZAPI_HOST,
    }
