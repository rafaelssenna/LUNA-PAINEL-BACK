from fastapi import APIRouter, Depends, HTTPException, Query
from ..utils.jwt_handler import decode_jwt
import httpx

router = APIRouter()

def base(host: str) -> str: return f"https://{host}"
def hdr(tok: str) -> dict:  return {"token": tok, "Content-Type": "application/json"}

@router.get("/instance/status")
async def instance_status(user=Depends(decode_jwt)):
    host, tok = user["host"], user["token"]
    url = f"{base(host)}/instance/status"
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, headers=hdr(tok))
        if r.status_code >= 400: raise HTTPException(r.status_code, r.text)
        return r.json()

@router.get("/labels")
async def labels(user=Depends(decode_jwt)):
    host, tok = user["host"], user["token"]
    url = f"{base(host)}/labels"
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, headers=hdr(tok))
        if r.status_code >= 400: raise HTTPException(r.status_code, r.text)
        return r.json()

@router.get("/chat/name-image")
async def chat_name_image(chatid: str = Query(..., min_length=5), user=Depends(decode_jwt)):
    host, tok = user["host"], user["token"]
    url  = f"{base(host)}/chat/GetNameAndImageURL"
    params = {"chatid": chatid}
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, headers=hdr(tok), params=params)
        if r.status_code >= 400: raise HTTPException(r.status_code, r.text)
        try:
            data = r.json()
        except Exception:
            data = {}
        name = data.get("name") or data.get("Name") or ""
        image = data.get("imageUrl") or data.get("ImageURL") or data.get("url") or ""
        return {"name": name, "imageUrl": image}
