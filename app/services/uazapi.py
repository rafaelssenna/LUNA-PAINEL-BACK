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

async def connect_instance(instance_id: str, token: str, max_retries: int = 3) -> Dict[str, Any]:
    """
    Conecta a instância e gera o QR Code.
    Endpoint oficial: POST /instance/connect

    Conforme docs.uazapi.com:
    - Header: "token" com o token da instância
    - Body vazio ou sem "phone" gera QR Code
    - Body com "phone" gera código de pareamento

    Args:
        instance_id: ID da instância
        token: Token da instância
        max_retries: Número máximo de tentativas (padrão: 3)

    Returns:
        {
            "qrcode": "data:image/png;base64,iVBOR...",
            "paircode": "1234-5678",
            ...
        }
    """
    import asyncio

    url = f"https://{UAZAPI_HOST}/instance/connect"

    log.info(f"🔄 [CONNECT] Conectando instância: {instance_id}")
    log.info(f"📤 [CONNECT] URL: {url}")
    log.info(f"📤 [CONNECT] Header token: {token[:20]}...")

    # Tentar múltiplas vezes com delay progressivo
    for attempt in range(1, max_retries + 1):
        try:
            log.info(f"🔄 [CONNECT] Tentativa {attempt}/{max_retries}")

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                # Não enviar phone para gerar QR Code
                response = await client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "token": token  # Header da instância
                    },
                    json={}  # Body vazio = gera QR code
                )

                log.info(f"📥 [CONNECT] Status: {response.status_code}")
                log.info(f"📥 [CONNECT] Response (primeiros 500): {response.text[:500]}")

                response.raise_for_status()
                data = response.json()

                # QR code pode estar em vários lugares dependendo da versão da API
                instance_data = data.get("instance", {})
                qrcode = instance_data.get("qrcode", "") or data.get("qrcode", "")
                paircode = instance_data.get("paircode", "") or data.get("paircode", "")

                log.info(f"✅ [CONNECT] Resposta recebida")
                log.info(f"📊 [CONNECT] QR code: presente={bool(qrcode)}, length={len(qrcode) if qrcode else 0}")
                log.info(f"📊 [CONNECT] Pair code: presente={bool(paircode)}")

                if qrcode:
                    log.info(f"🎉 [CONNECT] QR CODE GERADO COM SUCESSO!")
                    # Retornar imediatamente se conseguiu o QR code
                    return {
                        "qrcode": qrcode,
                        "paircode": paircode,
                        "status": instance_data.get("status", "connecting"),
                        "connected": data.get("connected", False)
                    }
                elif paircode:
                    log.info(f"🎉 [CONNECT] PAIR CODE GERADO: {paircode}")
                    return {
                        "qrcode": qrcode,
                        "paircode": paircode,
                        "status": instance_data.get("status", "connecting"),
                        "connected": data.get("connected", False)
                    }
                else:
                    log.warning(f"⚠️ [CONNECT] Tentativa {attempt}: Nenhum QR code ou pair code na resposta!")
                    log.warning(f"⚠️ [CONNECT] Response completo: {data}")

                    # Se não é a última tentativa, aguardar antes de tentar novamente
                    if attempt < max_retries:
                        wait_time = attempt * 2  # 2s, 4s, 6s...
                        log.info(f"⏳ [CONNECT] Aguardando {wait_time}s antes da próxima tentativa...")
                        await asyncio.sleep(wait_time)
                    else:
                        # Última tentativa falhou, retornar o que temos
                        log.error(f"❌ [CONNECT] Todas as {max_retries} tentativas falharam")
                        return {
                            "qrcode": "",
                            "paircode": "",
                            "status": instance_data.get("status", "disconnected"),
                            "connected": False,
                            "error": "QR code não disponível após múltiplas tentativas"
                        }

        except httpx.HTTPError as e:
            log.error(f"❌ [CONNECT] Erro HTTP na tentativa {attempt}: {e}")
            log.error(f"❌ [CONNECT] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")

            # Se não é a última tentativa, aguardar antes de tentar novamente
            if attempt < max_retries:
                wait_time = attempt * 2
                log.info(f"⏳ [CONNECT] Aguardando {wait_time}s antes de tentar novamente...")
                await asyncio.sleep(wait_time)
            else:
                raise UazapiError(f"Falha ao conectar instância após {max_retries} tentativas: {str(e)}")

    # Fallback (não deveria chegar aqui)
    raise UazapiError(f"Falha ao conectar instância")

async def fetch_instance_info(instance_id: str, token: str) -> Dict[str, Any]:
    """
    Busca informações da instância incluindo QR code.
    """
    url = f"https://{UAZAPI_HOST}/instance/fetchInstances"
    
    log.info(f"🔄 Buscando info da instância: {instance_id}")
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"token": token},
                params={"instanceName": instance_id}
            )
            
            log.info(f"📥 FetchInstances status: {response.status_code}")
            log.info(f"📥 FetchInstances response: {response.text[:500]}")
            
            response.raise_for_status()
            data = response.json()
            
            return data
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao buscar info da instância: {e}")
        return {}

async def get_qrcode(instance_id: str, token: str) -> Dict[str, Any]:
    """
    Busca o QR Code de uma instância.
    Tenta primeiro connect, depois fetch.
    """
    # Tentar connect primeiro
    connect_data = await connect_instance(instance_id, token)
    
    if connect_data.get("qrcode"):
        return connect_data
    
    # Se não veio QR code, tenta fetch
    log.info(f"🔄 QR code não veio no connect, tentando fetch...")
    fetch_data = await fetch_instance_info(instance_id, token)
    
    return fetch_data

async def get_connection_state(instance_id: str, token: str) -> Dict[str, Any]:
    """
    Verifica o estado da conexão de uma instância.
    
    Conforme docs.uazapi.com:
    Endpoint: GET /instance/status
    Header: token (token da instância)
    
    Returns:
        {
            "status": "connected",  # ou "connecting", "disconnected"
            "state": "open",  # ou "close", "connecting"
            ...
        }
    """
    url = f"https://{UAZAPI_HOST}/instance/status"
    
    try:
        log.info(f"🔍 [STATUS] Verificando status da instância: {instance_id}")
        log.info(f"🔍 [STATUS] URL: {url}")
        log.info(f"🔍 [STATUS] Token: {token[:20]}...")
        
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"token": token}
            )
            
            log.info(f"📥 [STATUS] Response status: {response.status_code}")
            log.info(f"📥 [STATUS] Response body: {response.text[:500]}")
            
            response.raise_for_status()
            data = response.json()
            
            # UAZAPI retorna: {"instance": {"status": "connected", ...}, "connected": bool}
            # O campo "connected" raiz pode estar desatualizado!
            # CONFIAR APENAS em instance.status (fonte oficial)
            instance_data = data.get("instance", {})
            status = instance_data.get("status", "disconnected")
            
            # Campos adicionais (podem estar desatualizados)
            connected_bool = data.get("connected", False)
            logged_in = data.get("loggedIn", False)
            
            log.info(f"📊 [STATUS] UAZAPI response:")
            log.info(f"   - instance.status: {status} ← FONTE OFICIAL")
            log.info(f"   - connected (bool raiz): {connected_bool} (pode estar desatualizado)")
            log.info(f"   - loggedIn: {logged_in}")
            
            # Retornar formato normalizado
            # CONFIAR APENAS em instance.status!
            return {
                "status": status,  # "disconnected", "connecting", ou "connected"
                "state": "open" if status == "connected" else "close",  # ✅ Baseado APENAS no status
                "connected": status == "connected",  # ✅ Derivado do status, não do campo raiz
                "loggedIn": logged_in,
                "instance": instance_data
            }
    except httpx.HTTPError as e:
        log.error(f"❌ [STATUS] Erro HTTP: {e}")
        log.error(f"❌ [STATUS] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
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
    url = f"https://{UAZAPI_HOST}/instance/{instance_id}"
    
    try:
        log.info(f"🔍 Buscando info da instância: {url}")
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"token": token}
            )
            response.raise_for_status()
            data = response.json()
            log.info(f"📥 Instance info response: {data}")
            return data
    except httpx.HTTPError as e:
        log.error(f"❌ Erro ao buscar info: {e}")
        log.error(f"❌ Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
        return None

async def set_webhook(instance_id: str, token: str, webhook_url: str) -> Dict[str, Any]:
    """
    Configura o webhook para receber mensagens.
    
    Conforme docs.uazapi.com - MODO SIMPLES:
    - Endpoint: POST /webhook
    - Header: token (token da instância)
    - Sem action/id (API cuida automaticamente)
    - Events: ["messages", "messages_update"]
    - excludeMessages: ["wasSentByApi", "isGroupYes"] para evitar loops e grupos
    
    Args:
        instance_id: ID da instância (para logs)
        token: Token da instância
        webhook_url: URL completa do webhook (ex: https://backend.com/api/webhook)
    """
    url = f"https://{UAZAPI_HOST}/webhook"
    
    log.info(f"🔗 [WEBHOOK] Configurando webhook para instância {instance_id}")
    log.info(f"🔗 [WEBHOOK] URL: {url}")
    log.info(f"🔗 [WEBHOOK] Webhook URL: {webhook_url}")
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "token": token  # Token da instância (não admintoken
                },
                json={
                    "enabled": True,
                    "url": webhook_url,
                    "events": [
                        "messages",            # Novas mensagens
                        "messages_update",     # Atualizações de mensagens
                        "connection_update",   # ✅ Eventos de conexão/desconexão
                        "status_update"        # ✅ Mudanças de status
                    ],
                    "excludeMessages": [
                        "wasSentByApi",  # Evitar loops (mensagens enviadas pela API)
                        "isGroupYes"     # Ignorar mensagens de grupos
                    ]
                }
            )
            
            log.info(f"📥 [WEBHOOK] Response status: {response.status_code}")
            log.info(f"📥 [WEBHOOK] Response body: {response.text[:500]}")
            
            response.raise_for_status()
            data = response.json()
            
            log.info(f"✅ [WEBHOOK] Webhook configurado com sucesso!")
            log.info(f"   - Instância: {instance_id}")
            log.info(f"   - URL: {webhook_url}")
            log.info(f"   - Eventos: messages, messages_update")
            log.info(f"   - Filtros: wasSentByApi, isGroupYes")
            
            return data
    except httpx.HTTPError as e:
        log.error(f"❌ [WEBHOOK] Erro HTTP: {e}")
        log.error(f"❌ [WEBHOOK] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
        raise UazapiError(f"Falha ao configurar webhook: {str(e)}")

async def get_webhook(instance_id: str, token: str) -> Optional[Dict[str, Any]]:
    """
    Verifica o webhook configurado para a instância.
    
    Conforme docs.uazapi.com:
    - Endpoint: GET /webhook
    - Header: token (token da instância)
    - Retorna lista de webhooks configurados
    """
    url = f"https://{UAZAPI_HOST}/webhook"
    
    log.info(f"🔍 [WEBHOOK] Verificando webhook da instância {instance_id}")
    
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(
                url,
                headers={"token": token}
            )
            
            log.info(f"📥 [WEBHOOK] Response status: {response.status_code}")
            log.info(f"📥 [WEBHOOK] Response body: {response.text[:500]}")
            
            response.raise_for_status()
            data = response.json()
            
            log.info(f"✅ [WEBHOOK] Webhook verificado:")
            log.info(f"   - Dados: {data}")
            
            return data
    except httpx.HTTPError as e:
        log.error(f"❌ [WEBHOOK] Erro ao verificar webhook: {e}")
        log.error(f"❌ [WEBHOOK] Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
        return None

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
