from fastapi import HTTPException, Header
import jwt, os

JWT_SECRET = os.getenv("LUNA_JWT_SECRET", "changeme")

def decode_jwt(authorization: str = Header(...)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer ausente")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if "host" not in payload or "token" not in payload:
            raise HTTPException(status_code=401, detail="JWT sem host/token")
        return payload
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"JWT inválido/expirado: {e}")
