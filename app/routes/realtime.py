from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from ..utils.jwt_handler import decode_jwt
import httpx

router = APIRouter()

@router.get("/sse")
async def sse(events: str = "chats,messages,messages_update", user=Depends(decode_jwt)):
    host, tok = user["host"], user["token"]
    url  = f"https://{host}/sse?token={tok}&events={events}"

    async def gen():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    text = await r.aread()
                    raise HTTPException(r.status_code, text.decode())
                async for line in r.aiter_lines():
                    if line is None: continue
                    yield (line + "\n")
    return StreamingResponse(gen(), media_type="text/event-stream")
