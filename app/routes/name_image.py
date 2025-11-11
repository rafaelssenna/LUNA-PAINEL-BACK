# app/routes/name_image.py
import json
import hashlib
import httpx
from fastapi import APIRouter, Depends, HTTPException, Body, Response, Request
from app.routes.deps import get_uazapi_ctx  # mesmo deps usado nas outras rotas

router = APIRouter()


def _uaz(ctx):
    base = f"https://{ctx['host']}"
    headers = {
        "token": ctx["token"],
        "accept": "application/json",
        "cache-control": "no-cache",
    }
    return base, headers


def _normalize(resp: dict) -> dict:
    """
    Normaliza a resposta da UAZAPI para sempre conter:
      { "name": str|None, "image": str|None, "imagePreview": str|None }
    """
    if not isinstance(resp, dict):
        return {"name": None, "image": None, "imagePreview": None}

    name = (
        resp.get("name")
        or resp.get("wa_name")
        or resp.get("fullName")
        or resp.get("displayName")
    )
    image = resp.get("image") or resp.get("photo") or resp.get("profileImage")
    image_preview = (
        resp.get("imagePreview")
        or resp.get("photoPreview")
        or resp.get("preview")
    )

    return {"name": name, "image": image, "imagePreview": image_preview}


async def _try(cli: httpx.AsyncClient, method: str, url: str, headers: dict, json=None, params=None):
    if method == "GET":
        return await cli.get(url, headers=headers, params=params)
    return await cli.post(url, headers=headers, json=json)


def _payload_is_url_expired(res: httpx.Response) -> bool:
    try:
        ct = (res.headers.get("content-type") or "").lower()
    except Exception:
        ct = ""
    if "application/json" in ct:
        try:
            res.json()
            return False  # JSON válido -> não é o caso
        except Exception:
            pass
    # não é JSON válido; checa texto
    try:
        txt = (res.text or "").strip().lower()
    except Exception:
        txt = ""
    return "url signature expired" in txt


# -------- Helpers de resposta cacheável --------
def _etag_for(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    h = hashlib.sha1(raw).hexdigest()
    return f'W/"{h}"'  # weak etag é suficiente

def _cacheable_json_response(request: Request, payload: dict, ttl: int) -> Response:
    etag = _etag_for(payload)
    inm = request.headers.get("if-none-match")
    if inm and inm == etag:
        resp = Response(status_code=304)
    else:
        resp = Response(
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            media_type="application/json",
            status_code=200,
        )
    resp.headers["ETag"] = etag
    # permitir reuso pelo navegador e CDNs; SWR ajuda a suavizar picos
    resp.headers["Cache-Control"] = f"public, max-age={ttl}, stale-while-revalidate={ttl}"
    return resp


@router.post("/name-image")
async def get_name_and_image(
    request: Request,
    payload: dict = Body(..., example={"number": "5531999999999@s.whatsapp.net", "preview": True}),
    ctx=Depends(get_uazapi_ctx),
):
    """
    Proxy para o endpoint de nome/foto da UAZAPI, com fallbacks de rota:
      - /chat/GetNameAndImageURL
      - /chat/GetNameAndImageUrl
      - /chat/getNameAndImageURL
      - /chat/getNameAndImageUrl
      - /chat/GetNameAndImage
      - /chat/getNameAndImage

    Body esperado:
      { "number": "<wa_chatid ou número>", "preview": true|false }

    Respostas possuem Cache-Control + ETag:
      - Sucesso:   max-age=86400 (24h)
      - Upstream “URL signature expired”: max-age=300 (5min)  -> evita martelar a API
    """
    number = (payload.get("number") or "").strip()
    preview = bool(payload.get("preview", True))
    if not number:
        raise HTTPException(status_code=400, detail="number é obrigatório")

    base, headers = _uaz(ctx)
    body = {"number": number, "preview": preview}
    params = {"number": number, "preview": "true" if preview else "false"}

    attempts = [
        ("POST", f"{base}/chat/GetNameAndImageURL", body, None),
        ("POST", f"{base}/chat/GetNameAndImageUrl", body, None),
        ("POST", f"{base}/chat/getNameAndImageURL", body, None),
        ("POST", f"{base}/chat/getNameAndImageUrl", body, None),
        ("POST", f"{base}/chat/GetNameAndImage", body, None),
        ("POST", f"{base}/chat/getNameAndImage", body, None),

        ("GET",  f"{base}/chat/GetNameAndImageURL", None, params),
        ("GET",  f"{base}/chat/GetNameAndImageUrl", None, params),
        ("GET",  f"{base}/chat/getNameAndImageURL", None, params),
        ("GET",  f"{base}/chat/getNameAndImageUrl", None, params),
        ("GET",  f"{base}/chat/GetNameAndImage", None, params),
        ("GET",  f"{base}/chat/getNameAndImage", None, params),
    ]

    last_status = 502
    last_text = "Upstream error"

    async with httpx.AsyncClient(timeout=25) as cli:
        for method, url, json_payload, query in attempts:
            r = await _try(cli, method, url, headers, json=json_payload, params=query)
            last_status, last_text = r.status_code, r.text

            if 200 <= r.status_code < 300:
                # Pode vir 200 com texto "URL signature expired" ou payload não-JSON
                if _payload_is_url_expired(r):
                    payload_null = {"name": None, "image": None, "imagePreview": None}
                    # TTL curto para não "congelar" vazio por muito tempo
                    return _cacheable_json_response(request, payload_null, ttl=300)

                try:
                    data = r.json()
                except Exception:
                    raise HTTPException(status_code=502, detail="Resposta inválida da UAZAPI em name-image")

                norm = _normalize(data)
                # sucesso normal: TTL de 24h
                return _cacheable_json_response(request, norm, ttl=86400)

            if r.status_code in (404, 405):
                continue  # tenta próxima rota

            # Qualquer outro erro: repassa
            raise HTTPException(status_code=r.status_code, detail=last_text)

    raise HTTPException(status_code=last_status, detail=last_text)
