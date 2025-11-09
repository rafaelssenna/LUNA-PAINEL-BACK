import os
import jwt as pyjwt
from fastapi import Depends, HTTPException, Request
from app.auth import get_current_user as get_instance_user

JWT_SECRET = os.getenv("LUNA_JWT_SECRET") or os.getenv("JWT_SECRET") or "change-me"
JWT_ALG = os.getenv("JWT_ALGORITHM", "HS256")

def get_current_user(request: Request) -> dict:
    """
    Autentica usuário do sistema (não instância) via JWT.
    Retorna dict com dados do usuário.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Authorization header ausente ou inválido")
    
    token = auth.split(" ", 1)[1].strip()
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        
        # Verifica se é JWT de usuário (não de admin ou instância)
        sub = payload.get("sub", "")
        if not sub.startswith("user:"):
            raise HTTPException(401, "Token não é de usuário")
        
        return payload
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Sessão expirada. Faça login novamente.")
    except pyjwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido. Faça login novamente.")

def get_uazapi_ctx(user=Depends(get_instance_user)) -> dict:
    """
    Extrai dados para falar com a UAZAPI de forma robusta.
    - token da instância: user['token'] OU user['instance_token']
    - host: user['host'] OU env UAZAPI_HOST
    """
    token = (user.get("token") or user.get("instance_token") or "").strip()
    host  = (user.get("host")  or os.getenv("UAZAPI_HOST") or "").strip()

    if not token:
        raise HTTPException(status_code=401, detail="JWT sem token de instância")
    if not host:
        raise HTTPException(status_code=401, detail="JWT sem host e UAZAPI_HOST não definido")

    return {"token": token, "host": host}
