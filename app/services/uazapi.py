"""
Cliente para integração com UAZAPI - WhatsApp API
"""
from __future__ import annotations
import os
import logging
from typing import Dict, Any, Optional
import httpx

log = logging.getLogger("uvicorn.error")

UAZAPI_HOST = os.getenv("UAZAPI_HOST", "hia-clientes.uazapi.com")
UAZAPI_ADMIN_TOKEN = os.getenv("UAZAPI_ADMIN_TOKEN", "")
DEFAULT_TIMEOUT = 30.0

class UazapiError(Exception):
    """Erro ao comunicar com UAZAPI"""
    pass

async def create_instance(instance_name: str) -> Dict[str, Any]:
    """
    Cria uma nova instância no UAZAPI.
    
    Returns:
        {
            "instance": {
                "instanceName": "luna_123_1699999999",
                "token": "abc123...",
                "status": "created"
            }
        }
    """
    if not UAZAPI_ADMIN_TOKEN:
        raise UazapiError("UAZAPI_ADMIN_TOKEN não configurado")
    
    url = f"https://{UAZAPI_HOST}/instance/create"
    
    log.info(f"🔄 Criando instância UAZAPI: {instance_name}")
    log.info(f"🔄 URL: {url}")
    log.info(f"🔄 Host: {UAZAPI_HOST}")
    log.info(f"🔄 Token presente: {bool(UAZAPI_ADMIN_TOKEN)} (length: {len(UAZAPI_ADMIN_TOKEN)})")
    
    # Headers base (sempre incluir)
    base_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Luna-Backend/1.0"
    }
    
    # Testar diferentes formatos de autenticação
    test_configs = [
        # (endpoint_path, auth_header_dict)
        ("/instance/create", {"apikey": UAZAPI_ADMIN_TOKEN}),
        ("/instance/create", {"Authorization": f"Bearer {UAZAPI_ADMIN_TOKEN}"}),
        ("/instance/create", {"x-api-key": UAZAPI_ADMIN_TOKEN}),
        ("/instance/create", {"admin_token": UAZAPI_ADMIN_TOKEN}),
        ("/instance/create", {"global_apikey": UAZAPI_ADMIN_TOKEN}),
        ("/instance/create", {"api-key": UAZAPI_ADMIN_TOKEN}),  # Com hífen
        ("/instance/create", {"Api-Key": UAZAPI_ADMIN_TOKEN}),  # Capitalizado
        ("/instance/create", {"token": UAZAPI_ADMIN_TOKEN}),  # Simples
    ]
    
    last_error = None
    
    for idx, (endpoint_path, auth_headers) in enumerate(test_configs):
        full_url = f"https://{UAZAPI_HOST}{endpoint_path}"
        
        # Mesclar headers base com headers de autenticação
        full_headers = {**base_headers, **auth_headers}
        
        log.info(f"🔄 Tentativa {idx + 1}/{len(test_configs)}")
        log.info(f"   URL: {full_url}")
        log.info(f"   Auth header: {list(auth_headers.keys())[0]}")
        
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    full_url,
                    headers=full_headers,
                    json={
                        "instanceName": instance_name,
                        "qrcode": True,
                        "integration": "WHATSAPP-BAILEYS"
                    }
                )
            
                log.info(f"📥 Response status: {response.status_code}")
                log.info(f"📥 Response text: {response.text[:500]}")  # Primeiros 500 chars
                
                response.raise_for_status()
                data = response.json()
                log.info(f"✅ Instância criada na UAZAPI: {instance_name}")
                log.info(f"✅ Response data keys: {list(data.keys())}")
                log.info(f"✅ Auth header que funcionou: {list(auth_headers.keys())[0]}")
                log.info(f"✅ Endpoint que funcionou: {endpoint_path}")
                return data
                
        except httpx.HTTPStatusError as e:
            last_error = e
            log.warning(f"⚠️ Tentativa {idx + 1} falhou: {e.response.status_code} - {e.response.text[:200]}")
            if idx < len(test_configs) - 1:
                continue  # Tentar próxima configuração
            # Se foi a última tentativa, raise
            log.error(f"❌ TODAS as {len(test_configs)} tentativas falharam!")
            log.error(f"❌ Último status: {e.response.status_code}")
            log.error(f"❌ Último response: {e.response.text[:500]}")
            raise UazapiError(f"Falha ao criar instância após {len(test_configs)} tentativas. Verifique token e endpoint no painel UAZAPI.")
        except Exception as e:
            last_error = e
            log.error(f"❌ Erro na tentativa {idx + 1}: {type(e).__name__}: {e}")
            if idx < len(test_configs) - 1:
                continue
            raise UazapiError(f"Erro inesperado após {len(test_configs)} tentativas: {str(e)}")

async def get_qrcode(instance_id: str, token: str) -> Dict[str, Any]:
    """
    Busca o QR Code de uma instância.
    
    Returns:
        {
            "qrcode": "data:image/png;base64,iVBOR...",
            "status": "qr_code"
        }
    """
    url = f"https://{UAZAPI_HOST}/instance/qrcode/{instance_id}"
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"apikey": token}
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao buscar QR code: {e}")
        raise UazapiError(f"Falha ao buscar QR Code: {str(e)}")

async def get_connection_state(instance_id: str, token: str) -> Dict[str, Any]:
    """
    Verifica o estado da conexão de uma instância.
    
    Returns:
        {
            "instance": "luna_123_1699999999",
            "state": "open",  # ou "close", "connecting"
            "statusConnection": "connected"
        }
    """
    url = f"https://{UAZAPI_HOST}/instance/connectionState/{instance_id}"
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"apikey": token}
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao verificar status: {e}")
        raise UazapiError(f"Falha ao verificar status: {str(e)}")

async def get_instance_info(instance_id: str, token: str) -> Optional[Dict[str, Any]]:
    """
    Busca informações detalhadas da instância (incluindo número do WhatsApp).
    
    Returns:
        {
            "instance": {
                "instanceName": "luna_123",
                "owner": "5511999999999@s.whatsapp.net",
                "profileName": "Nome do Usuário",
                "profilePictureUrl": "...",
                "state": "open"
            }
        }
    """
    url = f"https://{UAZAPI_HOST}/instance/info/{instance_id}"
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"apikey": token}
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        return None

async def set_webhook(instance_id: str, token: str, webhook_url: str) -> Dict[str, Any]:
    """
    Configura o webhook para receber mensagens.
    
    Args:
        instance_id: ID da instância
        token: Token da instância
        webhook_url: URL completa do webhook (ex: https://backend.com/api/webhook)
    """
    url = f"https://{UAZAPI_HOST}/webhook/set/{instance_id}"
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={"apikey": token},
                json={
                    "enabled": True,
                    "url": webhook_url,
                    "events": [
                        "MESSAGES_UPSERT",
                        "MESSAGES_UPDATE",
                        "CONNECTION_UPDATE"
                    ]
                }
            )
            response.raise_for_status()
            data = response.json()
            log.info(f"✅ Webhook configurado para {instance_id}: {webhook_url}")
            return data
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao configurar webhook: {e}")
        raise UazapiError(f"Falha ao configurar webhook: {str(e)}")

async def delete_instance(instance_id: str, token: str) -> Dict[str, Any]:
    """
    Deleta uma instância do UAZAPI.
    """
    url = f"https://{UAZAPI_HOST}/instance/delete/{instance_id}"
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.delete(
                url,
                headers={"apikey": token}
            )
            response.raise_for_status()
            log.info(f"✅ Instância deletada: {instance_id}")
            return response.json()
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao deletar instância: {e}")
        raise UazapiError(f"Falha ao deletar instância: {str(e)}")

async def send_text(instance_id: str, token: str, phone: str, message: str) -> Dict[str, Any]:
    """
    Envia mensagem de texto via UAZAPI.
    """
    url = f"https://{UAZAPI_HOST}/message/sendText/{instance_id}"
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={"apikey": token},
                json={
                    "number": phone,
                    "text": message
                }
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao enviar mensagem: {e}")
        raise UazapiError(f"Falha ao enviar mensagem: {str(e)}")

def extract_phone_from_owner(owner: str) -> Optional[str]:
    """
    Extrai número do telefone do formato 'owner' da UAZAPI.
    Ex: '5511999999999@s.whatsapp.net' -> '5511999999999'
    """
    if not owner:
        return None
    return owner.split("@")[0]
