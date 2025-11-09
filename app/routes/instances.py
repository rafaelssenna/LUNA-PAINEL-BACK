"""
Rotas para gerenciar instâncias WhatsApp (UAZAPI)
"""
from __future__ import annotations
from typing import Dict, Any, Optional
from datetime import datetime
import logging
import os
import uuid

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from app.pg import get_pool
from app.services import uazapi
from app.routes.deps import get_current_user

router = APIRouter()
log = logging.getLogger("uvicorn.error")

# URL do backend para webhook
BACKEND_URL = os.getenv("PUBLIC_BASE_URL", "https://lunahia.com.br")
WEBHOOK_URL = f"{BACKEND_URL.rstrip('/')}/api/webhook"

# ==============================================================================
# MODELOS
# ==============================================================================

class CreateInstanceOut(BaseModel):
    instance_id: str
    status: str
    qrcode: Optional[str] = None
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
                return CreateInstanceOut(
                    instance_id=existing["id"],
                    status=existing["status"],
                    message="Você já possui uma instância. Use /instances/{id}/qrcode para obter novo QR Code."
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
        
        # 3. Salvar no banco usando apenas colunas que existem
        try:
            with get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO instances (
                            id, user_id, uazapi_token, uazapi_host,
                            status, admin_status, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                        """,
                        (
                            db_instance_id,  # ← Usar ID da UAZAPI diretamente
                            user_id,
                            instance_token,
                            uazapi.UAZAPI_HOST,
                            "disconnected",
                            "pending_config"
                        )
                    )
                    conn.commit()
                    log.info(f"✅ Instância salva no banco: {db_instance_id}")
        except Exception as db_error:
            # Se falhar por UUID, logar mas não bloquear (instância já foi criada na UAZAPI)
            log.warning(f"⚠️ Falha ao salvar no banco (UUID): {db_error}")
            log.warning(f"⚠️ Instância criada na UAZAPI mas não salva no banco!")
            # Continuar mesmo assim para retornar o QR code
        
        # 4. Conectar instância e buscar QR Code
        qr_data = instance_data.get("qrcode")  # Tentar da resposta primeiro
        
        log.info(f"📊 QR code da criação: presente={bool(qr_data)}")
        
        if not qr_data:
            # QR code não veio na criação, usar get_qrcode que tenta múltiplos endpoints
            try:
                log.info(f"🔄 QR code vazio, tentando obter via endpoints...")
                qr_result = await uazapi.get_qrcode(instance_id, instance_token)
                qr_data = qr_result.get("qrcode")
                paircode = qr_result.get("paircode")
                
                if qr_data:
                    log.info(f"✅ QR code obtido! (length: {len(qr_data)})")
                elif paircode:
                    log.info(f"✅ Pair code obtido: {paircode}")
                else:
                    log.warning(f"⚠️ QR code não disponível ainda")
                    log.warning(f"⚠️ Usuário pode obter depois via /instances/{instance_id}/qrcode")
            except Exception as e:
                log.error(f"❌ Falha ao obter QR code: {e}")
        
        return {
            "instance_id": db_instance_id,  # ← Retornar ID da UAZAPI (para o frontend usar)
            "status": "disconnected",
            "qrcode": qr_data,
            "uazapi_token": instance_token,  # ← Retornar token para autenticação
            "message": "Instância criada! Escaneie o QR Code com seu WhatsApp."
        }
        
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
    
    # Buscar QR Code (instance_id já é o ID da UAZAPI)
    try:
        result = await uazapi.get_qrcode(instance_id, token)
        return {
            "instance_id": instance_id,
            "qrcode": result.get("qrcode"),
            "status": result.get("status", "qr_code")
        }
    except uazapi.UazapiError as e:
        raise HTTPException(500, str(e))

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
        state_result = await uazapi.get_connection_state(instance_id, token)
        uazapi_state = state_result.get("state", "close")
        
        connected = uazapi_state == "open"
        
        # Se conectou e ainda não temos o número, buscar
        if connected and not phone_number:
            info_result = await uazapi.get_instance_info(instance_id, token)
            if info_result:
                instance_info = info_result.get("instance", {})
                owner = instance_info.get("owner")
                if owner:
                    phone_number = uazapi.extract_phone_from_owner(owner)
                    
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
