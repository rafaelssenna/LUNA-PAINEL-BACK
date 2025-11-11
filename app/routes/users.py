from __future__ import annotations
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
import logging
import os
import jwt

from fastapi import APIRouter, HTTPException, Request, Body
from pydantic import BaseModel, EmailStr

from app.services.users import (
    create_user,
    get_user_by_email,
    verify_password,
    touch_last_login,
)
from app.pg import get_pool

# Tenta integrar com serviço de instâncias, se existir
try:
    # def attach_instance_to_user(user_id: int, instance_token: str) -> dict: ...
    from app.services.instances import attach_instance_to_user  # type: ignore
except Exception:  # pragma: no cover
    attach_instance_to_user = None  # fallback: segue sem persistir, apenas loga

router = APIRouter()
log = logging.getLogger("uvicorn.error")

# >>> Variáveis de ambiente padronizadas (suporta suas chaves atuais)
JWT_SECRET = os.getenv("LUNA_JWT_SECRET") or os.getenv("JWT_SECRET") or "change-me"
JWT_ALG = os.getenv("JWT_ALGORITHM", "HS256")

# Se LUNA_JWT_TTL existir, tratamos como SEGUNDOS; senão, USER_JWT_TTL_MIN (minutos).
if os.getenv("LUNA_JWT_TTL"):
    JWT_TTL_SECONDS = int(os.getenv("LUNA_JWT_TTL", "2592000"))
else:
    JWT_TTL_SECONDS = int(os.getenv("USER_JWT_TTL_MIN", "43200")) * 60  # 30 dias


def _issue_user_jwt(user: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "luna-backend",
        "sub": f"user:{user['id']}",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=JWT_TTL_SECONDS)).timestamp()),
        "email": user["email"],
        "role": "user",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


# ------------------------------------------------------------------------------
# Modelos
# ------------------------------------------------------------------------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class InstanceConnectIn(BaseModel):
    """
    Body para conectar uma instância à conta do usuário logado.
    - Envie apenas o token da instância no body.
    - O email do usuário é obtido do JWT (Authorization: Bearer <jwt>).
    """
    token: str


class InstanceData(BaseModel):
    """Dados da instância do usuário."""
    id: str
    token: str
    status: str
    phone_number: Optional[str] = None


class LoginOut(BaseModel):
    """Resposta do login com JWT da conta e instância (se existir)."""
    jwt: str  # JWT da conta do usuário
    instance_jwt: Optional[str] = None  # JWT da instância (se existir)
    instance: Optional[InstanceData] = None  # Dados da instância


# ------------------------------------------------------------------------------
# Helpers de autenticação
# ------------------------------------------------------------------------------
def _jwt_payload_from_request(req: Request) -> Dict[str, Any]:
    """
    Extrai e valida o JWT do header Authorization. Retorna o payload (dict).
    Levanta HTTP 401 se ausente ou inválido.
    """
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Authorization header ausente ou inválido")
    token = auth.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        return payload if isinstance(payload, dict) else {}
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Sessão expirada. Faça login novamente.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido. Faça login novamente.")


def _mask_token(t: str) -> str:
    """Ofusca token para log/response (exibe início e fim)."""
    if not t:
        return ""
    if len(t) <= 10:
        return t
    return f"{t[:6]}…{t[-4:]}"


# ------------------------------------------------------------------------------
# Rotas de usuário (conta)
# ------------------------------------------------------------------------------
@router.post("/register")
def register(body: RegisterIn):
    try:
        user = create_user(body.email, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = _issue_user_jwt({"id": user["id"], "email": user["email"]})
    return {"jwt": token, "profile": {"email": user["email"]}}


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn) -> LoginOut:
    """
    Login de usuário com e-mail e senha.
    Retorna JWT da conta + token da instância (se existir).
    """
    email = body.email.strip().lower()
    password = body.password.strip()
    
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Buscar usuário
            cur.execute(
                "SELECT id, email, password_hash FROM users WHERE email = %s",
                (email,)
            )
            row = cur.fetchone()
            
            if not row:
                raise HTTPException(status_code=401, detail="Credenciais inválidas")
            
            user_id = row["id"]
            stored_password = row["password_hash"]
            
            if not verify_password(password, stored_password):
                raise HTTPException(status_code=401, detail="Credenciais inválidas")
            
            # Buscar instância do usuário (se existir)
            cur.execute(
                """
                SELECT id, uazapi_token, status, phone_number 
                FROM instances 
                WHERE user_id = %s 
                LIMIT 1
                """,
                (user_id,)
            )
            instance = cur.fetchone()
            
            # Gerar JWT da conta
            payload = {
                "sub": f"user:{user_id}",
                "email": email,
                "user_email": email,
                "id": user_id,
                "iat": datetime.utcnow(),
                "exp": datetime.utcnow() + timedelta(days=30)
            }
            
            account_jwt = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
            
            # Se tem instância, gerar JWT dela também
            instance_jwt = None
            instance_data = None
            
            if instance:
                instance_token = instance["uazapi_token"]
                instance_id = instance["id"]
                
                # Gerar JWT da instância (para usar nas rotas)
                instance_payload = {
                    "token": instance_token,
                    "instance_token": instance_token,
                    "instance_id": instance_id,
                    "host": os.getenv("UAZAPI_HOST", ""),
                    "email": email,
                    "id": user_id,
                    "iat": datetime.utcnow(),
                    "exp": datetime.utcnow() + timedelta(days=30)
                }
                
                instance_jwt = jwt.encode(instance_payload, JWT_SECRET, algorithm=JWT_ALG)
                instance_data = {
                    "id": instance_id,
                    "token": instance_token,
                    "status": instance["status"],
                    "phone_number": instance["phone_number"]
                }
            
            return LoginOut(
                jwt=account_jwt,
                instance_jwt=instance_jwt,
                instance=instance_data
            )


@router.get("/instance")
def get_instance(request: Request):
    """
    Busca a instância do usuário autenticado.
    """
    # 1) Extrai e valida JWT, obtém e-mail
    payload = _jwt_payload_from_request(request)
    email = str(payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(401, "JWT sem e-mail. Faça login novamente.")

    # 2) Busca usuário no banco
    u = get_user_by_email(email)
    if not u:
        raise HTTPException(404, "Usuário não encontrado")

    # 3) Busca instância do usuário (se existir)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, uazapi_token, status, phone_number 
                FROM instances 
                WHERE user_id = %s 
                LIMIT 1
                """,
                (u["id"],)
            )
            instance = cur.fetchone()

            if instance:
                instance_data = {
                    "id": instance["id"],
                    "token": instance["uazapi_token"],
                    "status": instance["status"],
                    "phone_number": instance["phone_number"]
                }
                return {"instance": instance_data}
            else:
                return {"instance": None}


# ------------------------------------------------------------------------------
# Rotas de instância (vínculo da instância à conta)
# ------------------------------------------------------------------------------
@router.post("/instances/connect")
def connect_instance(request: Request, body: InstanceConnectIn = Body(...)):
    """
    Conecta uma instância à conta do usuário autenticado.
    """
    # 1) Extrai e valida JWT, obtém e-mail
    payload = _jwt_payload_from_request(request)
    email = str(payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(401, "JWT sem e-mail. Faça login novamente.")

    # 2) Busca usuário no banco
    u = get_user_by_email(email)
    if not u:
        raise HTTPException(404, "Usuário não encontrado")

    instance_token = body.token.strip()
    if not instance_token:
        raise HTTPException(422, "token da instância é obrigatório")

    # 3) Persiste vínculo user <-> instance (se serviço existir)
    if attach_instance_to_user:
        try:
            result = attach_instance_to_user(user_id=u["id"], instance_token=instance_token)  # type: ignore
        except Exception:
            log.exception("Falha ao vincular instância ao usuário")
            raise HTTPException(500, "Falha ao vincular instância")
    else:
        # Sem serviço de instância; não falha, mas avisa nos logs.
        log.warning(
            "attach_instance_to_user indisponível. Vínculo NÃO foi persistido. user=%s, token=%s",
            email, _mask_token(instance_token)
        )
        result = {"persisted": False, "note": "attach_instance_to_user ausente"}

    return {
        "ok": True,
        "user": {"id": u.get("id"), "email": email},
        "instance": {"token": _mask_token(instance_token)},
        "result": result,
    }
