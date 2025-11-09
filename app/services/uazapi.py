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
    
    log.info(f"🔄 Criando instância UAZAPI: {instance_name}")
    log.info(f"🔄 Host: {UAZAPI_HOST}")
    log.info(f"🔄 Token presente: {bool(UAZAPI_ADMIN_TOKEN)} (length: {len(UAZAPI_ADMIN_TOKEN)})")
    
    # Formato CORRETO da UAZAPI (documentação oficial)
    url = f"https://{UAZAPI_HOST}/instance/init"
    
    headers = {
        "Content-Type": "application/json",
        "admintoken": UAZAPI_ADMIN_TOKEN
    }
    
    body = {
        "name": instance_name,
        "systemName": "Luna-Platform"
    }
    
    log.info(f"📤 Requisição conforme documentação UAZAPI oficial:")
    log.info(f"   URL: {url}")
    log.info(f"   Headers: {list(headers.keys())}")
    log.info(f"   Body: {body}")
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                url,
                headers=headers,
                json=body
            )
        
        log.info(f"📥 Response status: {response.status_code}")
        log.info(f"📥 Response headers: {dict(response.headers)}")
        log.info(f"📥 Response text: {response.text}")
        
        if response.status_code == 401:
            log.error(f"❌ 401 Unauthorized - Admin Token rejeitado!")
            log.error(f"❌ Token usado: {UAZAPI_ADMIN_TOKEN[:15]}...")
            log.error(f"❌ TESTE MANUAL:")
            log.error(f"   curl -X POST '{url}' \\")
            log.error(f"     -H 'Content-Type: application/json' \\")
            log.error(f"     -H 'admintoken: {UAZAPI_ADMIN_TOKEN}' \\")
            log.error(f"     -d '{body}'")
            raise UazapiError("Admin Token rejeitado pela UAZAPI. Verifique se está correto no painel.")
        
        response.raise_for_status()
        data = response.json()
        
        log.info(f"✅ SUCESSO! Instância criada: {instance_name}")
        log.info(f"✅ Response message: {data.get('response', 'N/A')}")
        log.info(f"✅ Instance data: {data.get('instance', {})}")
        
        # Retornar no formato esperado pelo backend
        instance_data = data.get('instance', {})
        return {
            "instance": {
                "instanceName": instance_data.get('name', instance_name),
                "instanceId": instance_data.get('id', ''),
                "token": instance_data.get('token', ''),
                "status": instance_data.get('status', 'created'),
                "qrcode": instance_data.get('qrcode', ''),
                "paircode": instance_data.get('paircode', '')
            }
        }
        
    except httpx.HTTPStatusError as e:
        log.error(f"❌ Erro HTTP: {e.response.status_code}")
        log.error(f"❌ Response: {e.response.text}")
        raise UazapiError(f"Falha ao criar instância: {str(e)}")
    except Exception as e:
        log.error(f"❌ Erro inesperado: {type(e).__name__}: {e}")
        raise UazapiError(f"Erro inesperado: {str(e)}")

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
