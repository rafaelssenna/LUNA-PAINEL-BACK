"""
Rotas para gerenciar instâncias WhatsApp (UAZAPI)
"""
from __future__ import annotations
from typing import Dict, Any, Optional
from datetime import datetime
import logging
import os
import uuid
import httpx

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from app.pg import get_pool
from app.services import uazapi
from app.routes.deps import get_current_user

router = APIRouter()
log = logging.getLogger("uvicorn.error")

# URL do webhook (IMPORTANTE: Deve ser a URL do BACKEND, não do frontend!)
# Se não estiver no .env, usa o padrão do Railway
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://web-production-3bc4c.up.railway.app/api/webhook")

# Logar para debug
log.info(f"🔗 [WEBHOOK] URL configurada: {WEBHOOK_URL}")

# ==============================================================================
# MODELOS
# ==============================================================================

class CreateInstanceOut(BaseModel):
    instance_id: str
    status: str
    admin_status: str
    qrcode: Optional[str] = None
    uazapi_token: Optional[str] = None
    message: str

class InstanceStatusOut(BaseModel):
    instance_id: str
    status: str
    phone_number: Optional[str] = None
    admin_status: str
    connected: bool

# ==============================================================================
# ROTAS
# ==============================================================================

@router.post("/create")
async def create_instance_route(request: Request, user: Dict[str, Any] = Depends(get_current_user)):
    """
    Cria uma nova instância WhatsApp para o usuário logado.
    
    Fluxo:
    1. Verifica se usuário já tem instância
    2. Cria instância na UAZAPI
    3. Configura webhook automaticamente
    4. Salva no banco de dados
    5. Retorna QR Code
    """
    user_id = user["id"]
    
    # Verificar se já tem instância
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status FROM instances WHERE user_id = %s LIMIT 1",
                (user_id,)
            )
            existing = cur.fetchone()
            
            if existing:
                existing_id = existing["id"]
                existing_status = existing["status"]
                
                log.info(f"⚠️ [CREATE] Usuário {user_id} já tem instância: {existing_id}")
                log.info(f"⚠️ [CREATE] Status: {existing_status}")
                
                # Se já está conectada, não precisa de QR code
                if existing_status == "connected":
                    log.info(f"✅ [CREATE] Instância já conectada, redirecionando...")
                    return CreateInstanceOut(
                        instance_id=existing_id,
                        status=existing_status,
                        qrcode="",
                        message="Você já possui uma instância conectada!"
                    )
                
                # Se não está conectada, buscar novo QR code
                log.info(f"🔄 [CREATE] Instância desconectada, buscando QR code...")
                try:
                    # Buscar token da instância
                    cur.execute(
                        "SELECT uazapi_token FROM instances WHERE id = %s",
                        (existing_id,)
                    )
                    token_row = cur.fetchone()

                    if token_row and token_row["uazapi_token"]:
                        existing_token = token_row["uazapi_token"]

                        # Gerar novo QR code (com retry automático)
                        qr_result = await uazapi.get_qrcode(existing_id, existing_token)
                        qr_data = qr_result.get("qrcode", "")
                        qr_error = qr_result.get("error", "")

                        if qr_data:
                            log.info(f"✅ [CREATE] QR code obtido para instância existente!")

                            # Buscar admin_status
                            cur.execute(
                                "SELECT admin_status FROM instances WHERE id = %s",
                                (existing_id,)
                            )
                            admin_row = cur.fetchone()
                            admin_status = admin_row["admin_status"] if admin_row else "pending_config"

                            return CreateInstanceOut(
                                instance_id=existing_id,
                                status=existing_status,
                                admin_status=admin_status,
                                qrcode=qr_data,
                                uazapi_token=existing_token,
                                message="Instância encontrada! Escaneie o QR Code."
                            )
                        elif qr_error:
                            log.warning(f"⚠️ [CREATE] {qr_error}")
                except Exception as e:
                    log.error(f"❌ [CREATE] Erro ao buscar QR code da instância existente: {e}")
                    import traceback
                    log.error(f"❌ [CREATE] Traceback: {traceback.format_exc()}")
                
                # Fallback: retornar sem QR code
                return CreateInstanceOut(
                    instance_id=existing_id,
                    status=existing_status,
                    message="Você já possui uma instância. Acesse a aba Instâncias para obter QR Code."
                )
    
    # Gerar nome único para a instância
    timestamp = int(datetime.utcnow().timestamp())
    instance_name = f"luna_{user_id}_{timestamp}"
    
    try:
        # 1. Criar instância na UAZAPI
        result = await uazapi.create_instance(instance_name)
        instance_data = result.get("instance", {})
        instance_id = instance_data.get("instanceId")  # ID real da instância
        instance_token = instance_data.get("token")
        
        if not instance_id:
            raise HTTPException(500, "UAZAPI não retornou ID da instância")
        
        if not instance_token:
            raise HTTPException(500, "UAZAPI não retornou token da instância")
        
        log.info(f"✅ Instância criada - UAZAPI ID: {instance_id}, Name: {instance_name}, Token: {instance_token[:20]}...")
        
        # Usar o próprio instance_id da UAZAPI como identificador
        # O ID da UAZAPI será usado tanto no banco quanto nas chamadas API
        db_instance_id = instance_id
        
        # 2. Configurar webhook automaticamente (TODO: verificar endpoint correto)
        # try:
        #     await uazapi.set_webhook(instance_id, instance_token, WEBHOOK_URL)
        #     log.info(f"✅ Webhook configurado para {instance_id}")
        # except Exception as e:
        #     log.warning(f"⚠️ Falha ao configurar webhook para {instance_id}: {e}")
        
        # 3. Salvar no banco. Tentar incluir também as colunas `token` e `host` caso existam
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    try:
                        # Tentar inserir incluindo as colunas `token` e `host` (para compatibilidade com
                        # versões mais novas do schema). Caso essas colunas não existam, a instrução
                        # levantará uma exceção e o bloco abaixo fará o fallback para a versão antiga.
                        cur.execute(
                            """
                            INSERT INTO instances (
                                id, instance_id, user_id,
                                uazapi_token, uazapi_host,
                                token, host,
                                status, admin_status, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                            """,
                            (
                                db_instance_id,       # ID principal
                                db_instance_id,       # instance_id (duplicação)
                                user_id,
                                instance_token,       # uazapi_token
                                uazapi.UAZAPI_HOST,   # uazapi_host
                                instance_token,       # token (coluna compatível)
                                uazapi.UAZAPI_HOST,   # host  (coluna compatível)
                                "disconnected",
                                "pending_config"
                            )
                        )
                        conn.commit()
                        log.info(f"✅ Instância salva no banco (com token/host): {db_instance_id}")
                    except Exception as insert_error:
                        # Fallback: inserir sem as colunas `token` e `host`
                        log.warning(f"⚠️ Falha ao inserir com token/host: {insert_error}. Tentando inserção antiga...")
                        cur.execute(
                            """
                            INSERT INTO instances (
                                id, instance_id, user_id, uazapi_token, uazapi_host,
                                status, admin_status, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                            """,
                            (
                                db_instance_id,
                                db_instance_id,
                                user_id,
                                instance_token,
                                uazapi.UAZAPI_HOST,
                                "disconnected",
                                "pending_config"
                            )
                        )
                        conn.commit()
                        log.info(f"✅ Instância salva no banco (sem token/host): {db_instance_id}")
        except Exception as db_error:
            # Se falhar por UUID ou outra razão, logar mas não bloquear (instância já foi criada na UAZAPI)
            log.warning(f"⚠️ Falha ao salvar no banco (UUID): {db_error}")
            log.warning(f"⚠️ Instância criada na UAZAPI mas não salva no banco!")
            # Continuar mesmo assim para retornar o QR code
        
        # 4. Conectar instância e buscar QR Code
        qr_data = instance_data.get("qrcode")  # Tentar da resposta primeiro

        log.info(f"📊 [CREATE] QR code na resposta de criação: presente={bool(qr_data)}")

        if not qr_data:
            # QR code não veio na criação, aguardar um pouco e chamar /instance/connect
            try:
                import asyncio

                # Aguardar 3 segundos para a instância ficar pronta
                log.info(f"⏳ [CREATE] Aguardando 3s para instância ficar pronta...")
                await asyncio.sleep(3)

                log.info(f"🔄 [CREATE] QR code vazio, chamando /instance/connect...")
                qr_result = await uazapi.get_qrcode(instance_id, instance_token)
                qr_data = qr_result.get("qrcode")
                paircode = qr_result.get("paircode")

                if qr_data:
                    log.info(f"✅ [CREATE] QR code obtido! (length: {len(qr_data)})")
                elif paircode:
                    log.info(f"✅ [CREATE] Pair code obtido: {paircode}")
                else:
                    log.warning(f"⚠️ [CREATE] QR code não disponível ainda")
                    log.warning(f"⚠️ [CREATE] Usuário pode obter depois via /instances/{instance_id}/qrcode")
            except Exception as e:
                log.error(f"❌ [CREATE] Falha ao obter QR code: {e}")
                import traceback
                log.error(f"❌ [CREATE] Traceback: {traceback.format_exc()}")
        
        # 4. Iniciar trial de 14 dias automaticamente (por e-mail)
        try:
            from app.services.billing import ensure_trial, canonical_email_key
            user_email = user.get("email", "")
            if user_email:
                billing_key = canonical_email_key(user_email)
                ensure_trial(billing_key)
                log.info(f"✅ Trial de 14 dias iniciado para {user_email}")
        except Exception as e:
            log.warning(f"⚠️ Falha ao iniciar trial: {e}")
        
        response_data = {
            "instance_id": db_instance_id,
            "status": "disconnected",
            "admin_status": "pending_config",
            "qrcode": qr_data if qr_data else "",
            "uazapi_token": instance_token,
            "message": "Instância criada! Escaneie o QR Code com seu WhatsApp."
        }
        
        log.info(f"📤 [CREATE] Retornando para frontend:")
        log.info(f"   - instance_id: {response_data['instance_id']}")
        log.info(f"   - status: {response_data['status']}")
        log.info(f"   - qrcode: presente={bool(response_data['qrcode'])}, length={len(response_data['qrcode']) if response_data['qrcode'] else 0}")
        log.info(f"   - uazapi_token: {response_data['uazapi_token'][:20]}...")
        
        return response_data
        
    except uazapi.UazapiError as e:
        raise HTTPException(500, f"Erro ao criar instância: {str(e)}")
    except Exception as e:
        log.error(f"❌ Erro inesperado ao criar instância: {e}")
        raise HTTPException(500, "Erro interno ao criar instância")

@router.get("/{instance_id}/qrcode")
async def get_qrcode_route(
    instance_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Retorna o QR Code de uma instância.

    Fluxo UAZAPI (conforme docs.uazapi.com):
    1. POST /instance/connect gera QR Code (válido por 2 minutos)
    2. Status fica "connecting" após escanear
    3. Status muda para "connected" quando finalizado
    4. Se QR expirar, este endpoint regerará automaticamente

    Agora com retry automático para maior confiabilidade!
    """
    user_id = user["id"]

    # Verificar permissão (instance_id já é o ID da UAZAPI)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT uazapi_token, status FROM instances WHERE id = %s AND user_id = %s",
                (instance_id, user_id)
            )
            row = cur.fetchone()

            if not row:
                raise HTTPException(404, "Instância não encontrada")

            token = row["uazapi_token"]
            status = row["status"]

            if status == "connected":
                return {
                    "instance_id": instance_id,
                    "status": "connected",
                    "message": "WhatsApp já está conectado!"
                }

    # Buscar/Regerar QR Code (sempre chama /instance/connect para QR novo)
    # Agora com retry automático (até 3 tentativas)
    try:
        log.info(f"🔄 [QRCODE] Gerando QR Code para instância {instance_id}")
        result = await uazapi.get_qrcode(instance_id, token)

        qrcode = result.get("qrcode", "")
        qr_error = result.get("error", "")

        log.info(f"📊 [QRCODE] QR Code gerado: presente={bool(qrcode)}, length={len(qrcode) if qrcode else 0}")

        if qrcode:
            return {
                "instance_id": instance_id,
                "qrcode": qrcode,
                "status": result.get("status", "qr_code"),
                "message": "QR Code válido por 2 minutos. Escaneie com WhatsApp Business."
            }
        else:
            # QR code não disponível mesmo após retry
            error_msg = qr_error or "QR Code não disponível no momento. Tente novamente em alguns segundos."
            return {
                "instance_id": instance_id,
                "qrcode": "",
                "status": "error",
                "message": error_msg
            }

    except uazapi.UazapiError as e:
        log.error(f"❌ [QRCODE] Erro ao gerar QR Code: {e}")
        raise HTTPException(500, f"Erro ao gerar QR Code: {str(e)}")

@router.get("/{instance_id}/status", response_model=InstanceStatusOut)
async def get_status_route(
    instance_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Verifica o status da conexão de uma instância.
    
    Se detectar que conectou, busca o número do WhatsApp e atualiza o banco.
    """
    user_id = user["id"]
    
    # Buscar no banco (instance_id já é o ID da UAZAPI)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT uazapi_token, status, admin_status, phone_number 
                FROM instances 
                WHERE id = %s AND user_id = %s
                """,
                (instance_id, user_id)
            )
            row = cur.fetchone()
            
            if not row:
                raise HTTPException(404, "Instância não encontrada")
            
            token = row["uazapi_token"]
            current_status = row["status"]
            admin_status = row["admin_status"]
            phone_number = row["phone_number"]
    
    # Verificar status na UAZAPI (instance_id já é o ID correto)
    try:
        log.info(f"🔍 [STATUS] Verificando status da instância {instance_id}")
        log.info(f"🔍 [STATUS] Token: {token[:20]}... | Status atual no banco: {current_status}")
        
        state_result = await uazapi.get_connection_state(instance_id, token)
        
        # Conforme docs UAZAPI: status pode ser "disconnected", "connecting" ou "connected"
        uazapi_status = state_result.get("status", "")
        uazapi_state = state_result.get("state", "close")
        
        log.info(f"📊 [STATUS] UAZAPI retornou:")
        log.info(f"   - status: {uazapi_status}")
        log.info(f"   - state: {uazapi_state}")
        
        # Conectado APENAS se status="connected" E state="open"
        # Durante "connecting" o usuário escaneou mas ainda não finalizou
        is_connecting = (uazapi_status == "connecting")
        connected = (uazapi_status == "connected") and (uazapi_state == "open")
        
        log.info(f"📊 [STATUS] Connecting? {is_connecting} | Connected? {connected} | Tem phone_number? {bool(phone_number)}")
        
        # Se conectou e ainda não temos o número, extrair do owner
        if connected and not phone_number:
            log.info(f"📞 [STATUS] Extraindo número do telefone...")
            
            # Owner já vem na resposta de status dentro de instance
            instance_info = state_result.get("instance", {})
            owner = instance_info.get("owner")
            log.info(f"👤 [STATUS] Owner na resposta: {owner}")
            
            if owner:
                # Owner pode vir como "553188379840" ou "553188379840@s.whatsapp.net"
                phone_number = uazapi.extract_phone_from_owner(owner)
                log.info(f"📱 [STATUS] Número extraído: {phone_number}")
                
                # Atualizar banco
                with get_pool().connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE instances 
                            SET status = %s, phone_number = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            ("connected", phone_number, instance_id)
                        )
                        conn.commit()
                
                current_status = "connected"
                log.info(f"✅ Instância {instance_id} conectada com número {phone_number}")
                
                # ✅ CONFIGURAR WEBHOOK AUTOMATICAMENTE
                try:
                    log.info(f"🔗 [WEBHOOK] Configurando webhook automaticamente...")
                    webhook_result = await uazapi.set_webhook(
                        instance_id=instance_id,
                        token=token,
                        webhook_url=WEBHOOK_URL
                    )
                    log.info(f"✅ [WEBHOOK] Webhook configurado com sucesso!")
                except Exception as webhook_error:
                    # Não falhar se webhook der erro, mas logar
                    log.error(f"⚠️ [WEBHOOK] Erro ao configurar webhook: {webhook_error}")
                    log.error(f"⚠️ [WEBHOOK] Instância funcionará, mas webhook precisa ser configurado manualmente")
                
            else:
                log.warning(f"⚠️ [STATUS] Owner não veio na resposta do status!")
        
        elif not connected and current_status == "connected":
            # Desconectou
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE instances SET status = %s, updated_at = NOW() WHERE id = %s",
                        ("disconnected", instance_id)
                    )
                    conn.commit()
            current_status = "disconnected"
        
        return InstanceStatusOut(
            instance_id=instance_id,
            status=current_status,
            phone_number=phone_number,
            admin_status=admin_status,
            connected=connected
        )
        
    except uazapi.UazapiError as e:
        # Se falhar, retornar status do banco
        return InstanceStatusOut(
            instance_id=instance_id,
            status=current_status,
            phone_number=phone_number,
            admin_status=admin_status,
            connected=current_status == "connected"
        )

@router.delete("/{instance_id}")
async def delete_instance_route(
    instance_id: str,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Deleta uma instância.
    """
    user_id = user["id"]
    
    # Verificar permissão
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT uazapi_token FROM instances WHERE id = %s AND user_id = %s",
                (instance_id, user_id)
            )
            row = cur.fetchone()
            
            if not row:
                raise HTTPException(404, "Instância não encontrada")
            
            token = row["uazapi_token"]
    
    # Deletar na UAZAPI
    try:
        await uazapi.delete_instance(instance_id, token)
    except uazapi.UazapiError as e:
        log.warning(f"⚠️ Falha ao deletar instância na UAZAPI: {e}")
    
    # Deletar do banco
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM instances WHERE id = %s", (instance_id,))
            conn.commit()
    
    return {"message": "Instância deletada com sucesso"}

@router.get("/my-instances")
async def list_my_instances(request: Request, user: Dict[str, Any] = Depends(get_current_user)):
    """
    Lista todas as instâncias do usuário logado.
    """
    user_id = user["id"]
    
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status, admin_status, phone_number, created_at
                FROM instances
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,)
            )
            rows = cur.fetchall()
            
            return {
                "instances": [
                    {
                        "id": row["id"],
                        "status": row["status"],
                        "admin_status": row["admin_status"],
                        "phone_number": row["phone_number"],
                        "created_at": row["created_at"].isoformat() if row.get("created_at") else None
                    }
                    for row in rows
                ]
            }

@router.get("/my-status")
async def get_my_instance_status(request: Request, user: Dict[str, Any] = Depends(get_current_user)):
    """
    Retorna o status da instância do usuário logado.
    Usado pelo frontend para verificar se WhatsApp está conectado e se admin já configurou.
    """
    user_id = user["id"]
    
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Tentar buscar campos completos (incluindo colunas compatíveis)
            # Se as colunas não existirem, o SELECT falhará e o bloco de exceção
            # executará um SELECT mais simples.
            try:
                cur.execute(
                    """
                    SELECT id, instance_id, status, admin_status, phone_number,
                           prompt, redirect_phone,
                           token, host,
                           uazapi_token, uazapi_host
                    FROM instances
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id,)
                )
            except Exception as e:
                # Se campos não existirem, buscar apenas os essenciais
                print(f"[STATUS] Campos opcionais ausentes, realizando SELECT reduzido: {e}")
                cur.execute(
                    """
                    SELECT id, instance_id, status, admin_status, phone_number,
                           prompt, redirect_phone
                    FROM instances
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id,)
                )
            
            row = cur.fetchone()
            
            if not row:
                return {
                    "has_instance": False,
                    "message": "Você ainda não criou uma instância. Crie uma para começar!"
                }
            
            instance_status = row["status"]
            admin_status = row["admin_status"]
            phone_number = row.get("phone_number")
            has_prompt = bool(row.get("prompt"))
            has_redirect = bool(row.get("redirect_phone"))

            # Obter token/host compatíveis. Os campos `token` e `host` são preferidos,
            # mas se não existirem ou estiverem vazios, caímos para uazapi_token/host
            instance_token = row.get("token") or row.get("uazapi_token") or ""
            instance_host = row.get("host") or row.get("uazapi_host") or ""

            # Se não houver host salvo, usar variável de ambiente
            if not instance_host:
                instance_host = os.getenv("UAZAPI_HOST", "")

            # Verificar status real na UAZAPI apenas se tivermos um token disponível
            # Utilizamos a função utilitária de services.uazapi para garantir um parsing
            # correto da resposta, confiando no campo instance.status ao invés do
            # booleano "connected" raiz.
            real_status = instance_status  # Fallback caso não possamos verificar
            if instance_token:
                try:
                    # O ID da instância pode ser encontrado em `instance_id` ou `id`
                    target_id = row.get("instance_id") or row.get("id")
                    state_data = await uazapi.get_connection_state(target_id, instance_token)
                    status_from_uazapi = state_data.get("status", "disconnected")
                    # Consideramos conectado apenas se o status for "connected"
                    if status_from_uazapi == "connected":
                        real_status = "connected"
                    else:
                        real_status = "disconnected"
                    
                    # Atualizar no banco se mudou
                    if real_status != instance_status:
                        cur.execute(
                            "UPDATE instances SET status = %s WHERE id = %s",
                            (real_status, row["id"])
                        )
                        conn.commit()
                        print(f"[STATUS] Instância {row['id']} atualizada: {instance_status} -> {real_status}")
                except Exception as e:
                    print(f"[STATUS] Erro ao consultar a UAZAPI: {e}")
                    # Em caso de falha, mantemos o status do banco
            else:
                print(f"[STATUS] Sem token salvo, usando status do banco: {instance_status}")

            # Usar o status atualizado (real) daqui em diante
            instance_status = real_status
            
            # Determinar mensagem baseada no status
            if instance_status != "connected":
                message = "⏳ WhatsApp não conectado. Escaneie o QR Code para continuar."
                banner_type = "warning"
            elif admin_status == "pending_config":
                message = "✅ WhatsApp conectado! ⏳ Aguardando configuração da equipe Helsen."
                banner_type = "info"
            elif admin_status in ["configured", "active"]:
                message = "🎉 Sua Luna está ativa! Suas conversas estão sendo gerenciadas pela IA."
                banner_type = "success"
            elif admin_status == "suspended":
                message = "⚠️ Instância suspensa. Entre em contato com o suporte."
                banner_type = "error"
            else:
                message = "Status desconhecido. Entre em contato com o suporte."
                banner_type = "warning"
            
            return {
                "has_instance": True,
                "instance_id": row["id"],
                "status": instance_status,
                "admin_status": admin_status,
                "phone_number": phone_number,
                "is_connected": instance_status == "connected",
                "is_configured": admin_status in ["configured", "active"],
                "is_active": admin_status == "active",
                "has_prompt": has_prompt,
                "has_redirect": has_redirect,
                "message": message,
                "banner_type": banner_type
            }
