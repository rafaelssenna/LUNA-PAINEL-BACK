# ROTAS ADMINISTRATIVAS - PAINEL ADMIN
from __future__ import annotations
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
import logging
import os
import jwt
import bcrypt
import json
import csv
import io
import re

from fastapi import APIRouter, HTTPException, Depends, Header, UploadFile, File, BackgroundTasks
from pydantic import BaseModel, EmailStr
import httpx
import asyncio

from app.pg import get_pool
from app.services import uazapi

router = APIRouter()
log = logging.getLogger("uvicorn.error")

JWT_SECRET = os.getenv("LUNA_JWT_SECRET") or os.getenv("JWT_SECRET") or "change-me"
JWT_ALG = os.getenv("JWT_ALGORITHM", "HS256")
JWT_TTL_SECONDS = 86400  # 24 horas para admin
DEFAULT_DAILY_LIMIT = 30

# ==============================================================================
# MODELOS
# ==============================================================================

class AdminLoginIn(BaseModel):
    email: str  # Aceita qualquer string (ex: "admin")
    password: str

class AdminLoginOut(BaseModel):
    jwt: str
    profile: Dict[str, Any]

class ConfigureInstanceIn(BaseModel):
    prompt: str
    notes: str = ""
    redirect_phone: str = ""  # ✅ Número para handoff específico dessa Luna


class ContactIn(BaseModel):
    name: str
    phone: str
    niche: Optional[str] = None
    region: Optional[str] = None


class QueueActionIn(BaseModel):
    mark_sent: bool = False


class AutomationSettingsIn(BaseModel):
    daily_limit: int = 30
    auto_run: bool = False
    ia_auto: bool = False
    message_template: Optional[str] = None
    redirect_phone: Optional[str] = None

# ==============================================================================
# HELPERS
# ==============================================================================

PHONE_DIGITS_ONLY = re.compile(r"\D+")


def _normalize_phone(value: Optional[str]) -> str:
    if not value:
        return ""
    digits = PHONE_DIGITS_ONLY.sub("", value)
    if digits.startswith("55") and len(digits) > 13:
        digits = digits[:13]
    return digits


def _validate_phone_or_raise(phone: str):
    digits = _normalize_phone(phone)
    if len(digits) < 10:
        raise HTTPException(status_code=400, detail="Telefone inválido")
    return digits


def _ensure_instance_exists(conn, instance_id: str):
    with conn.cursor() as cur:
        cur.execute("SELECT id, user_id FROM instances WHERE id = %s", (instance_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instância não encontrada")
        return row


def _log_admin_action(conn, admin_id: int, action: str, instance_id: str, description: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description)
            VALUES (%s, %s, 'instance', %s, %s)
            """,
            (admin_id, action, instance_id, description),
        )

# ==============================================================================
# AUTENTICAÇÃO ADMIN
# ==============================================================================

def _issue_admin_jwt(admin: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "luna-backend",
        "sub": f"admin:{admin['id']}",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=JWT_TTL_SECONDS)).timestamp()),
        "email": admin["email"],
        "role": "admin",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

async def get_current_admin(authorization: str = Header(None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Não autenticado")
    
    token = authorization.split(" ")[1]
    
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Acesso negado")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessão expirada")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

# ==============================================================================
# ROTAS
# ==============================================================================

@router.post("/login", response_model=AdminLoginOut)
async def admin_login(body: AdminLoginIn):
    """Login de administrador"""
    
    with get_pool().connection() as conn:
        admin = conn.execute(
            "SELECT * FROM admin_users WHERE email = %s AND is_active = TRUE",
            (body.email,)
        ).fetchone()
        
        if not admin:
            raise HTTPException(status_code=401, detail="Credenciais inválidas")
        
        # Verificar senha
        password_ok = bcrypt.checkpw(
            body.password.encode('utf-8'),
            admin['password_hash'].encode('utf-8')
        )
        
        if not password_ok:
            raise HTTPException(status_code=401, detail="Credenciais inválidas")
        
        # Atualizar last_login
        conn.execute(
            "UPDATE admin_users SET last_login_at = NOW() WHERE id = %s",
            (admin['id'],)
        )
        
        token = _issue_admin_jwt(admin)
        
        return AdminLoginOut(
            jwt=token,
            profile={
                "id": admin['id'],
                "email": admin['email'],
                "full_name": admin['full_name'],
                "role": admin['role']
            }
        )

@router.get("/stats")
async def get_stats(admin: Dict = Depends(get_current_admin)):
    """Estatísticas do dashboard"""
    
    with get_pool().connection() as conn:
        stats = conn.execute("SELECT * FROM v_admin_stats").fetchone()
        
        return {
            "total_instances": stats['total_instances'] or 0,
            "pending_config": stats['pending_config'] or 0,
            "active_instances": stats['active_instances'] or 0,
            "connected_instances": stats['connected_instances'] or 0,
            "total_users": stats['total_users'] or 0,
            "users_on_trial": stats['users_on_trial'] or 0,
            "paying_users": stats['paying_users'] or 0,
            "messages_today": stats['messages_today'] or 0
        }

@router.get("/instances/pending")
async def get_pending_instances(admin: Dict = Depends(get_current_admin)):
    """
    Instâncias aguardando configuração.
    Inclui informações do questionário do usuário para facilitar configuração.
    """
    
    with get_pool().connection() as conn:
        rows = conn.execute("""
            SELECT 
                i.id as instance_uuid,
                i.instance_id,
                i.phone_number,
                i.created_at,
                u.email as user_email,
                u.full_name as user_name,
                q.company_name,
                q.contact_phone,
                q.contact_email,
                q.product_service,
                q.target_audience,
                q.notification_phone,
                q.prospecting_region,
                q.has_whatsapp_number,
                EXTRACT(EPOCH FROM (NOW() - i.created_at))/3600 as hours_waiting
            FROM instances i
            JOIN users u ON i.user_id = u.id
            LEFT JOIN user_questionnaires q ON q.user_id = u.id
            WHERE i.admin_status = 'pending_config'
            ORDER BY i.created_at ASC
        """).fetchall()
        
        return [
            {
                "instance_uuid": str(row['instance_uuid']),
                "instance_id": row['instance_id'],
                "user_email": row['user_email'],
                "user_name": row['user_name'],
                "phone_number": row['phone_number'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                "hours_waiting": float(row['hours_waiting']) if row['hours_waiting'] else 0,
                # Informações do questionário
                "questionnaire": {
                    "company_name": row['company_name'],
                    "contact_phone": row['contact_phone'],
                    "contact_email": row['contact_email'],
                    "product_service": row['product_service'],
                    "target_audience": row['target_audience'],
                    "notification_phone": row['notification_phone'],
                    "prospecting_region": row['prospecting_region'],
                    "has_whatsapp_number": row['has_whatsapp_number']
                } if row['company_name'] else None
            }
            for row in rows
        ]

@router.get("/instances/active")
async def get_active_instances(admin: Dict = Depends(get_current_admin)):
    """Instâncias ativas"""
    
    with get_pool().connection() as conn:
        rows = conn.execute("""
            SELECT 
                i.id, i.instance_id, i.admin_status, i.status,
                i.phone_number, i.phone_name, i.created_at,
                u.email as user_email, u.full_name as user_name,
                COUNT(DISTINCT s.id) as total_sessions,
                COUNT(DISTINCT m.id) as total_messages
            FROM instances i
            JOIN users u ON i.user_id = u.id
            LEFT JOIN sessions s ON i.instance_id = s.instance_id
            LEFT JOIN messages m ON i.instance_id = m.instance_id
            WHERE i.admin_status = 'active'
            GROUP BY i.id, u.id
            ORDER BY i.created_at DESC
        """).fetchall()
        
        return [
            {
                "id": str(row['id']),
                "instance_id": row['instance_id'],
                "admin_status": row['admin_status'],
                "status": row['status'],
                "phone_number": row['phone_number'],
                "phone_name": row['phone_name'],
                "user_email": row['user_email'],
                "user_name": row['user_name'],
                "total_sessions": row['total_sessions'],
                "total_messages": row['total_messages'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None
            }
            for row in rows
        ]

@router.get("/instances/all")
async def get_all_instances(admin: Dict = Depends(get_current_admin)):
    """Todas as instâncias"""
    
    with get_pool().connection() as conn:
        rows = conn.execute("""
            SELECT 
                i.id, i.instance_id, i.admin_status, i.status,
                i.phone_number, i.phone_name, i.created_at,
                u.email as user_email, u.full_name as user_name,
                COUNT(DISTINCT s.id) as total_sessions,
                COUNT(DISTINCT m.id) as total_messages
            FROM instances i
            JOIN users u ON i.user_id = u.id
            LEFT JOIN sessions s ON i.instance_id = s.instance_id
            LEFT JOIN messages m ON i.instance_id = m.instance_id
            GROUP BY i.id, u.id
            ORDER BY i.created_at DESC
        """).fetchall()
        
        return [
            {
                "id": str(row['id']),
                "instance_id": row['instance_id'],
                "admin_status": row['admin_status'],
                "status": row['status'],
                "phone_number": row['phone_number'],
                "phone_name": row['phone_name'],
                "user_email": row['user_email'],
                "user_name": row['user_name'],
                "total_sessions": row['total_sessions'],
                "total_messages": row['total_messages'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                "status_display": {
                    'pending_config': '🟡 Aguardando Config',
                    'configured': '🟢 Configurada',
                    'active': '✅ Ativa',
                    'suspended': '🔴 Suspensa'
                }.get(row['admin_status'], row['admin_status'])
            }
            for row in rows
        ]

@router.get("/instances/{instance_id}")
async def get_instance_detail(instance_id: str, admin: Dict = Depends(get_current_admin)):
    """Detalhes de uma instância"""
    
    with get_pool().connection() as conn:
        row = conn.execute("""
            SELECT 
                i.*, u.email as user_email, u.full_name as user_name
            FROM instances i
            JOIN users u ON i.user_id = u.id
            WHERE i.id = %s
        """, (instance_id,)).fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Instância não encontrada")
        
        return {
            "id": str(row['id']),
            "instance_id": row['instance_id'],
            "admin_status": row['admin_status'],
            "status": row['status'],
            "phone_number": row['phone_number'],
            "phone_name": row['phone_name'],
            "prompt": row['prompt'],
            "admin_notes": row['admin_notes'],
            "user_email": row['user_email'],
            "user_name": row['user_name'],
            "created_at": row['created_at'].isoformat() if row['created_at'] else None
        }

@router.post("/instances/{instance_id}/configure")
async def configure_instance(
    instance_id: str,
    body: ConfigureInstanceIn,
    admin: Dict = Depends(get_current_admin)
):
    """Configurar e ativar uma instância"""
    
    log.info(f"=" * 80)
    log.info(f"[ADMIN CONFIG] Requisição recebida!")
    log.info(f"  Instance ID: {instance_id}")
    log.info(f"  Admin: {admin.get('sub')}")
    log.info(f"  Prompt Length: {len(body.prompt)} caracteres")
    log.info(f"  Redirect Phone: {body.redirect_phone}")
    log.info(f"=" * 80)
    
    try:
        admin_id = int(admin['sub'].split(':')[1])
        log.info(f"[ADMIN CONFIG] Admin ID extraído: {admin_id}")
    except Exception as e:
        log.error(f"[ADMIN CONFIG] Erro ao extrair admin_id: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar admin: {str(e)}")
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Buscar instância
                log.info(f"[ADMIN CONFIG] Buscando instância {instance_id}...")
                cur.execute(
                    "SELECT * FROM instances WHERE id = %s",
                    (instance_id,)
                )
                instance = cur.fetchone()
                
                if not instance:
                    log.warning(f"[ADMIN CONFIG] Instância {instance_id} não encontrada!")
                    raise HTTPException(status_code=404, detail="Instância não encontrada")
                
                log.info(f"[ADMIN CONFIG] Instância encontrada: {instance['id']}, user={instance['user_id']}")
                
                # Salvar prompt anterior no histórico
                old_prompt = instance['prompt']
                
                # Converter prompt_history de JSONB para list
                prompt_history = []
                if instance['prompt_history']:
                    if isinstance(instance['prompt_history'], str):
                        prompt_history = json.loads(instance['prompt_history'])
                    elif isinstance(instance['prompt_history'], list):
                        prompt_history = instance['prompt_history']
                    else:
                        prompt_history = []
                
                # Adicionar mudança ao histórico
                if old_prompt:
                    prompt_history.append({
                        "changed_at": datetime.now(timezone.utc).isoformat(),
                        "changed_by": admin_id,
                        "old_prompt": old_prompt,
                        "new_prompt": body.prompt
                    })
                
                # Converter history para JSON string
                prompt_history_json = json.dumps(prompt_history)
                
                # Validar redirect_phone obrigatório
                if not body.redirect_phone or not body.redirect_phone.strip():
                    raise HTTPException(status_code=400, detail="Número para handoff é obrigatório")
                
                log.info(f"🔧 [ADMIN] Configurando instância {instance_id}")
                log.info(f"   - Prompt: {len(body.prompt)} caracteres")
                log.info(f"   - Redirect Phone: {body.redirect_phone}")
                log.info(f"   - Admin Status: active")

                # Atualizar instância - setar como 'active' para permitir que IA responda imediatamente
                cur.execute("""
                    UPDATE instances
                    SET
                        admin_status = 'active',
                        configured_by = %s,
                        configured_at = NOW(),
                        prompt = %s,
                        admin_notes = %s,
                        prompt_history = %s::jsonb,
                        redirect_phone = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (admin_id, body.prompt, body.notes, prompt_history_json, body.redirect_phone, instance_id))

                # ✅ TAMBÉM atualizar instance_settings para manter redirect_phone sincronizado
                cur.execute("""
                    INSERT INTO instance_settings (instance_id, redirect_phone, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (instance_id)
                    DO UPDATE SET
                        redirect_phone = EXCLUDED.redirect_phone,
                        updated_at = NOW()
                """, (instance_id, body.redirect_phone))

                log.info(f"✅ [CONFIGURE] redirect_phone sincronizado em ambas as tabelas: '{body.redirect_phone}'")

                # Registrar ação
                cur.execute("""
                    INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description)
                    VALUES (%s, 'configure_instance', 'instance', %s, 'Prompt configurado e instância ativada')
                """, (admin_id, instance_id))
                
                # Notificar usuário
                cur.execute("""
                    INSERT INTO notifications (recipient_type, recipient_id, type, title, message)
                    VALUES ('user', %s, 'instance_configured', 'Sua Luna está ativa!',
                            'Sua Luna foi configurada pela equipe Helsen e já está operacional!')
                """, (instance['user_id'],))
            
            # ✅ COMMIT DAS MUDANÇAS! (fora do cursor, dentro da conexão)
            conn.commit()
            log.info(f"✅ [ADMIN] Instância {instance_id} configurada e ativada com sucesso!")
                
        return {"ok": True, "message": "Instância configurada com sucesso"}
    
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"❌ [ADMIN CONFIG] Erro ao configurar instância: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao configurar instância: {str(e)}")

@router.get("/instances/{instance_id}")
async def get_instance_details(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """Buscar detalhes completos de uma instância (incluindo prompt)"""

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        i.id,
                        i.user_id,
                        i.phone_number,
                        i.status,
                        i.admin_status,
                        i.prompt,
                        i.redirect_phone,
                        i.admin_notes,
                        i.configured_at,
                        i.created_at,
                        i.updated_at,
                        u.email as user_email,
                        u.name as user_name
                    FROM instances i
                    LEFT JOIN users u ON i.user_id = u.id
                    WHERE i.id = %s
                """, (instance_id,))

                row = cur.fetchone()

                if not row:
                    raise HTTPException(status_code=404, detail="Instância não encontrada")

                log.info(f"📥 [GET INSTANCE] Carregando instância {instance_id}")
                log.info(f"   redirect_phone no banco: '{row['redirect_phone']}'")

                return {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "phone_number": row["phone_number"],
                    "status": row["status"],
                    "admin_status": row["admin_status"],
                    "prompt": row["prompt"],
                    "redirect_phone": row["redirect_phone"],
                    "admin_notes": row["admin_notes"],
                    "configured_at": row["configured_at"].isoformat() if row["configured_at"] else None,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "user_email": row["user_email"],
                    "user_name": row["user_name"]
                }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Erro ao buscar instância: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/instances/{instance_id}/suspend")
async def suspend_instance(
    instance_id: str,
    body: Dict,
    admin: Dict = Depends(get_current_admin)
):
    """Suspender uma instância (parar IA)"""
    
    reason = body.get("reason", "Suspensa pelo admin")
    admin_id = int(admin['sub'].split(':')[1])
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM instances WHERE id = %s", (instance_id,))
                instance = cur.fetchone()
                
                if not instance:
                    raise HTTPException(status_code=404, detail="Instância não encontrada")
                
                log.info(f"⚠️ [ADMIN] Suspendendo instância {instance_id}: {reason}")
                
                # Suspender (muda admin_status para suspended)
                cur.execute("""
                    UPDATE instances
                    SET 
                        admin_status = 'suspended',
                        admin_notes = COALESCE(admin_notes || E'\\n', '') || '[' || NOW() || '] Suspensa: ' || %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (reason, instance_id))
                
                # Registrar ação
                cur.execute("""
                    INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description, created_at)
                    VALUES (%s, 'suspend_instance', 'instance', %s, %s, NOW())
                """, (admin_id, instance_id, f'Instância suspensa: {reason}'))
                
                conn.commit()
                
                log.info(f"✅ [ADMIN] Instância {instance_id} suspensa com sucesso")
                
        return {"ok": True, "message": "Instância suspensa - IA não processará mais mensagens"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Erro ao suspender instância: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/instances/{instance_id}/activate")
async def activate_instance(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """Reativar uma instância suspensa"""
    
    admin_id = int(admin['sub'].split(':')[1])
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, admin_status FROM instances WHERE id = %s", (instance_id,))
                instance = cur.fetchone()
                
                if not instance:
                    raise HTTPException(status_code=404, detail="Instância não encontrada")
                
                log.info(f"✅ [ADMIN] Reativando instância {instance_id}")
                
                # Reativar
                cur.execute("""
                    UPDATE instances
                    SET 
                        admin_status = 'active',
                        updated_at = NOW()
                    WHERE id = %s
                """, (instance_id,))
                
                # Registrar ação
                cur.execute("""
                    INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description, created_at)
                    VALUES (%s, 'activate_instance', 'instance', %s, 'Instância reativada', NOW())
                """, (admin_id, instance_id))
                
                conn.commit()
                
                log.info(f"✅ [ADMIN] Instância {instance_id} reativada")
                
        return {"ok": True, "message": "Instância reativada"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Erro ao reativar instância: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/instances/{instance_id}/prompt")
async def update_prompt(
    instance_id: str,
    body: Dict,
    admin: Dict = Depends(get_current_admin)
):
    """Atualizar apenas o prompt de uma instância"""
    
    new_prompt = body.get("prompt", "").strip()
    
    if not new_prompt:
        raise HTTPException(status_code=400, detail="Prompt não pode ser vazio")
    
    admin_id = int(admin['sub'].split(':')[1])
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Buscar prompt anterior
                cur.execute("SELECT prompt FROM instances WHERE id = %s", (instance_id,))
                instance = cur.fetchone()
                
                if not instance:
                    raise HTTPException(status_code=404, detail="Instância não encontrada")
                
                old_prompt = instance['prompt'] if instance else ''
                
                log.info(f"📝 [ADMIN] Atualizando prompt da instância {instance_id}")
                log.info(f"   Prompt anterior: {len(old_prompt or '')} caracteres")
                log.info(f"   Prompt novo: {len(new_prompt)} caracteres")
                
                # Atualizar prompt
                cur.execute("""
                    UPDATE instances
                    SET 
                        prompt = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (new_prompt, instance_id))
                
                # Registrar ação
                cur.execute("""
                    INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description, created_at)
                    VALUES (%s, 'update_prompt', 'instance', %s, 'Prompt atualizado', NOW())
                """, (admin_id, instance_id))
                
                conn.commit()
                
                log.info(f"✅ [ADMIN] Prompt atualizado com sucesso!")
                
        return {"ok": True, "message": "Prompt atualizado com sucesso"}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Erro ao atualizar prompt: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/instances/{instance_id}/memory")
async def clear_instance_memory(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """
    Limpar memória (histórico de mensagens) da IA
    Útil quando o prompt é alterado e se quer começar do zero
    """
    
    admin_id = int(admin['sub'].split(':')[1])
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Verificar se instância existe
                cur.execute("SELECT id, phone_number FROM instances WHERE id = %s", (instance_id,))
                instance = cur.fetchone()
                
                if not instance:
                    raise HTTPException(status_code=404, detail="Instância não encontrada")
                
                log.info(f"🧹 [ADMIN] Limpando memória da instância {instance_id}")
                
                # Contar mensagens antes
                cur.execute("SELECT COUNT(*) FROM messages WHERE instance_id = %s", (instance_id,))
                count_before = cur.fetchone()['count']
                
                # Deletar todas as mensagens
                cur.execute("DELETE FROM messages WHERE instance_id = %s", (instance_id,))
                
                # Deletar todas as sessões
                cur.execute("DELETE FROM sessions WHERE instance_id = %s", (instance_id,))
                
                # Registrar ação
                cur.execute("""
                    INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description, created_at)
                    VALUES (%s, 'clear_memory', 'instance', %s, %s, NOW())
                """, (admin_id, instance_id, f'Memória limpa - {count_before} mensagens deletadas'))
                
                conn.commit()
                
                log.info(f"✅ [ADMIN] Memória limpa: {count_before} mensagens deletadas")
                
        return {
            "ok": True,
            "message": f"Memória limpa com sucesso! {count_before} mensagens deletadas.",
            "deleted_messages": count_before
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Erro ao limpar memória: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/instances/{instance_id}")
async def delete_instance(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """Remove definitivamente uma instância e dados relacionados."""

    try:
        admin_id = int(admin['sub'].split(':')[1])
    except (KeyError, ValueError, IndexError):
        raise HTTPException(status_code=401, detail="Admin inválido no token")

    # Buscar dados da instância
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, uazapi_token, token, phone_number
                FROM instances
                WHERE id = %s
                """,
                (instance_id,)
            )
            instance = cur.fetchone()

            if not instance:
                raise HTTPException(status_code=404, detail="Instância não encontrada")

            instance_user_id = instance["user_id"]
            instance_phone = instance.get("phone_number")
            instance_token = instance.get("uazapi_token") or instance.get("token")

    # Remover na UAZAPI
    if instance_token:
        try:
            await uazapi.delete_instance(instance_id, instance_token)
            log.info(f"🗑️ [ADMIN] Instância {instance_id} removida na UAZAPI")
        except uazapi.UazapiError as e:
            log.warning(f"⚠️ [ADMIN] Falha ao remover instância {instance_id} na UAZAPI: {e}")
    else:
        log.warning(f"⚠️ [ADMIN] Instância {instance_id} sem token para remoção na UAZAPI")

    # Remover dados locais
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE instance_id = %s", (instance_id,))
            cur.execute("DELETE FROM sessions WHERE instance_id = %s", (instance_id,))
            cur.execute("DELETE FROM lead_status WHERE instance_id = %s", (instance_id,))
            cur.execute("DELETE FROM ai_memory WHERE instance_id = %s", (instance_id,))
            cur.execute("UPDATE billing_accounts SET instance_id = NULL WHERE instance_id = %s", (instance_id,))
            cur.execute("DELETE FROM instances WHERE id = %s", (instance_id,))
            cur.execute(
                """
                INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description, created_at)
                VALUES (%s, 'delete_instance', 'instance', %s, %s, NOW())
                """,
                (
                    admin_id,
                    instance_id,
                    f"Instância deletada (user_id={instance_user_id}, phone={instance_phone or 'N/A'})"
                ),
            )
            conn.commit()

    return {"ok": True, "message": "Instância deletada com sucesso"}


# ============================================================================== 
# AUTOMAÇÃO POR INSTÂNCIA (Fila, Totais, Contatos, CSV, Progresso)
# ==============================================================================


def _resolve_instance_id(instance_id: Optional[str], conn) -> str:
    if instance_id:
        return instance_id

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM instances
             WHERE admin_status = 'active'
             ORDER BY created_at ASC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Nenhuma instância ativa encontrada")
        return row["id"]


@router.get("/instances/{instance_id}/queue")
async def get_instance_queue(
    instance_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    search: Optional[str] = None,
    admin: Dict = Depends(get_current_admin)
):
    if page < 1 or page_size < 1:
        raise HTTPException(status_code=400, detail="Paginação inválida")

    with get_pool().connection() as conn:
        resolved_id = _resolve_instance_id(instance_id, conn)

        _ensure_instance_exists(conn, resolved_id)

        params: List[Any] = [resolved_id]
        where_clause = ""
        if search:
            search = f"%{search.lower()}%"
            params.extend([search, search])
            where_clause = " AND (LOWER(name) LIKE %s OR phone LIKE %s)"

        offset = (page - 1) * page_size
        params.extend([page_size, offset])

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT phone, name, niche, region, created_at
                  FROM instance_queue
                 WHERE instance_id = %s {where_clause}
                 ORDER BY created_at ASC
                 LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()

            count_params = params[:1]
            if search:
                count_params.extend(params[1:3])

            cur.execute(
                f"""
                SELECT COUNT(*) as count
                  FROM instance_queue
                 WHERE instance_id = %s {where_clause}
                """,
                count_params,
            )
            total = cur.fetchone()["count"]

        items = [
            {
                "phone": row["phone"],
                "name": row["name"],
                "niche": row["niche"],
                "region": row["region"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "instance_id": resolved_id,
    }


@router.delete("/instances/{instance_id}/queue/{phone}")
async def queue_remove_or_mark(
    instance_id: str,
    phone: str,
    payload: QueueActionIn,
    admin: Dict = Depends(get_current_admin)
):
    digits = _validate_phone_or_raise(phone)

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM instance_queue WHERE instance_id = %s AND phone = %s",
                (instance_id, digits),
            )

            if payload.mark_sent:
                cur.execute(
                    """
                    UPDATE instance_totals
                       SET mensagem_enviada = TRUE, updated_at = NOW()
                     WHERE instance_id = %s AND phone = %s
                    """,
                    (instance_id, digits),
                )

        conn.commit()  # ✅ Necessário com autocommit=False

    return {"ok": True}


@router.post("/instances/{instance_id}/queue/{phone}/mark-sent")
async def queue_mark_sent(
    instance_id: str,
    phone: str,
    admin: Dict = Depends(get_current_admin)
):
    return await queue_remove_or_mark(instance_id, phone, QueueActionIn(mark_sent=True), admin)


@router.get("/instances/{instance_id}/totals")
async def get_instance_totals(
    instance_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    search: Optional[str] = None,
    sent: Optional[str] = None,
    admin: Dict = Depends(get_current_admin)
):
    if page < 1 or page_size < 1:
        raise HTTPException(status_code=400, detail="Paginação inválida")

    with get_pool().connection() as conn:
        resolved_id = _resolve_instance_id(instance_id, conn)
        _ensure_instance_exists(conn, resolved_id)

        where = ["instance_id = %s"]
        params: List[Any] = [resolved_id]

        if search:
            search = f"%{search.lower()}%"
            where.append("(LOWER(name) LIKE %s OR phone LIKE %s OR COALESCE(niche, '') ILIKE %s)")
            params.extend([search, search, search])

        if sent == "sim":
            where.append("mensagem_enviada = TRUE")
        elif sent == "nao":
            where.append("mensagem_enviada = FALSE")

        where_clause = " AND ".join(where)
        offset = (page - 1) * page_size

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT phone, name, niche, region, mensagem_enviada, updated_at
                  FROM instance_totals
                 WHERE {where_clause}
                 ORDER BY updated_at DESC
                 LIMIT %s OFFSET %s
                """,
                (*params, page_size, offset),
            )
            rows = cur.fetchall()

            cur.execute(
                f"SELECT COUNT(*) as count FROM instance_totals WHERE {where_clause}",
                params,
            )
            total = cur.fetchone()["count"]

    items = [
        {
            "phone": row["phone"],
            "name": row["name"],
            "niche": row["niche"],
            "region": row["region"],
            "mensagem_enviada": row["mensagem_enviada"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "instance_id": resolved_id,
    }


@router.post("/instances/{instance_id}/contacts")
async def add_contact_to_instance(
    instance_id: str,
    payload: ContactIn,
    admin: Dict = Depends(get_current_admin)
):
    digits = _validate_phone_or_raise(payload.phone)

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instance_totals (instance_id, phone, name, niche, region, mensagem_enviada, updated_at)
                VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
                ON CONFLICT (instance_id, phone)
                DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, instance_totals.name),
                    niche = COALESCE(EXCLUDED.niche, instance_totals.niche),
                    region = COALESCE(EXCLUDED.region, instance_totals.region),
                    mensagem_enviada = instance_totals.mensagem_enviada,
                    updated_at = NOW()
                RETURNING mensagem_enviada
                """,
                (instance_id, digits, payload.name, payload.niche, payload.region),
            )
            already_sent = cur.fetchone()["mensagem_enviada"]

            if already_sent:
                status = "skipped_already_sent"
            else:
                cur.execute(
                    """
                    INSERT INTO instance_queue (instance_id, phone, name, niche, region, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (instance_id, phone)
                    DO NOTHING
                    """,
                    (instance_id, digits, payload.name, payload.niche, payload.region),
                )
                status = "inserted" if cur.rowcount > 0 else "skipped_conflict"

        conn.commit()  # ✅ Necessário com autocommit=False

    return {"status": status}


@router.post("/instances/{instance_id}/import")
async def import_contacts_csv(
    instance_id: str,
    file: UploadFile = File(...),
    admin: Dict = Depends(get_current_admin)
):
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="Envie um arquivo CSV")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    inserted = skipped = errors = 0

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

        for row in reader:
            phone = row.get("phone") or row.get("telefone") or ""
            name = row.get("name") or row.get("nome") or phone
            niche = row.get("niche") or row.get("nicho")
            region = row.get("region") or row.get("regiao")

            if not phone:
                skipped += 1
                continue

            try:
                await add_contact_to_instance(
                    instance_id,
                    ContactIn(name=name, phone=phone, niche=niche, region=region),
                    admin,
                )
                inserted += 1
            except HTTPException as exc:
                if exc.status_code == 400:
                    skipped += 1
                else:
                    errors += 1

    return {"inserted": inserted, "skipped": skipped, "errors": errors}


@router.get("/instances/{instance_id}/progress")
async def get_instance_progress(
    instance_id: Optional[str] = None,
    admin: Dict = Depends(get_current_admin)
):
    with get_pool().connection() as conn:
        resolved_id = _resolve_instance_id(instance_id, conn)
        _ensure_instance_exists(conn, resolved_id)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE mensagem_enviada = TRUE) AS enviados,
                    COUNT(*) FILTER (WHERE mensagem_enviada = FALSE) AS pendentes
                  FROM instance_totals
                 WHERE instance_id = %s
                """,
                (resolved_id,),
            )
            row = cur.fetchone()

            cur.execute(
                """
                SELECT SUM(CASE WHEN mensagem_enviada THEN 1 ELSE 0 END) AS sent_today
                  FROM instance_totals
                 WHERE instance_id = %s AND mensagem_enviada = TRUE AND updated_at::date = CURRENT_DATE
                """,
                (resolved_id,),
            )
            sent_today_row = cur.fetchone()
            sent_today = sent_today_row["sent_today"] if sent_today_row and sent_today_row["sent_today"] is not None else 0

            cur.execute(
                "SELECT daily_limit, auto_run, ia_auto, message_template, redirect_phone FROM instance_settings WHERE instance_id = %s",
                (resolved_id,),
            )
            settings = cur.fetchone()

    total_enviados = row["enviados"] if row else 0
    pendentes = row["pendentes"] if row else 0
    daily_limit = settings["daily_limit"] if settings else DEFAULT_DAILY_LIMIT

    remaining = max(0, daily_limit - sent_today)
    pct = min(100, int((sent_today / daily_limit) * 100)) if daily_limit > 0 else 0

    return {
        "instance_id": resolved_id,
        "enviados": total_enviados,
        "pendentes": pendentes,
        "sent_today": sent_today,
        "daily_limit": daily_limit,
        "remaining": remaining,
        "percentage": pct,
        "auto_run": settings["auto_run"] if settings else False,
        "ia_auto": settings["ia_auto"] if settings else False,
        "message_template": settings["message_template"] if settings else "",
        "redirect_phone": settings["redirect_phone"] if settings else "",
        "now": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/instances/{instance_id}/settings")
async def update_instance_settings(
    instance_id: str,
    payload: AutomationSettingsIn,
    admin: Dict = Depends(get_current_admin)
):
    log.info(f"🔧 [SETTINGS] Salvando configurações para instância {instance_id}")
    log.info(f"   redirect_phone recebido: '{payload.redirect_phone}'")

    if payload.daily_limit <= 0:
        raise HTTPException(status_code=400, detail="daily_limit deve ser maior que zero")

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

        with conn.cursor() as cur:
            # ✅ Normalizar valores: string vazia ou só espaços → None
            redirect_phone_value = None
            if payload.redirect_phone:
                stripped = payload.redirect_phone.strip()
                redirect_phone_value = stripped if stripped else None

            message_template_value = None
            if payload.message_template:
                stripped = payload.message_template.strip()
                message_template_value = stripped if stripped else None

            log.info(f"   redirect_phone_value normalizado: '{redirect_phone_value}'")

            # Atualizar instance_settings
            cur.execute(
                """
                INSERT INTO instance_settings (instance_id, daily_limit, auto_run, ia_auto, message_template, redirect_phone, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (instance_id)
                DO UPDATE SET
                    daily_limit = EXCLUDED.daily_limit,
                    auto_run = EXCLUDED.auto_run,
                    ia_auto = EXCLUDED.ia_auto,
                    message_template = EXCLUDED.message_template,
                    redirect_phone = EXCLUDED.redirect_phone,
                    updated_at = NOW()
                """,
                (
                    instance_id,
                    payload.daily_limit,
                    payload.auto_run,
                    payload.ia_auto,
                    message_template_value,
                    redirect_phone_value,
                ),
            )

            # ✅ SEMPRE atualizar instances.redirect_phone para manter sincronizado
            cur.execute(
                """
                UPDATE instances
                SET redirect_phone = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (redirect_phone_value, instance_id)
            )

            log.info(f"✅ [SETTINGS] Configurações salvas:")
            log.info(f"   - instance_settings.redirect_phone = '{redirect_phone_value}'")
            log.info(f"   - instances.redirect_phone = '{redirect_phone_value}'")

        conn.commit()  # ✅ Necessário com autocommit=False

    return {"ok": True}


@router.get("/activity")
async def get_activity(admin: Dict = Depends(get_current_admin)):
    """Log de atividades recentes"""
    
    with get_pool().connection() as conn:
        rows = conn.execute("""
            SELECT 
                a.*, u.full_name as admin_name
            FROM admin_actions a
            JOIN admin_users u ON a.admin_id = u.id
            ORDER BY a.created_at DESC
            LIMIT 50
        """).fetchall()
        
        return [
            {
                "id": row['id'],
                "action_type": row['action_type'],
                "description": row['description'],
                "admin_name": row['admin_name'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None
            }
            for row in rows
        ]


@router.get("/questionnaires")
async def get_questionnaires(admin: Dict = Depends(get_current_admin)):
    """
    Lista todos os questionários dos usuários com informações da instância.
    Útil para o admin configurar a Luna com base nas respostas do cliente.
    """
    
    with get_pool().connection() as conn:
        rows = conn.execute("""
            SELECT 
                q.*,
                u.email as user_email,
                i.id as instance_id,
                i.phone_number,
                i.status as instance_status,
                i.admin_status
            FROM user_questionnaires q
            JOIN users u ON q.user_id = u.id
            LEFT JOIN instances i ON i.user_id = u.id
            ORDER BY q.created_at DESC
        """).fetchall()
        
        return [
            {
                "id": row['id'],
                "user_id": row['user_id'],
                "user_email": row['user_email'],
                "instance_id": row['instance_id'],
                "phone_number": row['phone_number'],
                "instance_status": row['instance_status'],
                "admin_status": row['admin_status'],
                "has_whatsapp_number": row['has_whatsapp_number'],
                "company_name": row['company_name'],
                "contact_phone": row['contact_phone'],
                "contact_email": row['contact_email'],
                "product_service": row['product_service'],
                "target_audience": row['target_audience'],
                "notification_phone": row['notification_phone'],
                "prospecting_region": row['prospecting_region'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                "updated_at": row['updated_at'].isoformat() if row['updated_at'] else None
            }
            for row in rows
        ]


@router.get("/questionnaires/{user_id}")
async def get_user_questionnaire(user_id: int, admin: Dict = Depends(get_current_admin)):
    """
    Busca o questionário de um usuário específico.
    """
    
    with get_pool().connection() as conn:
        row = conn.execute("""
            SELECT 
                q.*,
                u.email as user_email,
                i.id as instance_id,
                i.phone_number,
                i.status as instance_status,
                i.admin_status
            FROM user_questionnaires q
            JOIN users u ON q.user_id = u.id
            LEFT JOIN instances i ON i.user_id = u.id
            WHERE q.user_id = %s
        """, (user_id,)).fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Questionário não encontrado")
        
        return {
            "id": row['id'],
            "user_id": row['user_id'],
            "user_email": row['user_email'],
            "instance_id": row['instance_id'],
            "phone_number": row['phone_number'],
            "instance_status": row['instance_status'],
            "admin_status": row['admin_status'],
            "has_whatsapp_number": row['has_whatsapp_number'],
            "company_name": row['company_name'],
            "contact_phone": row['contact_phone'],
            "contact_email": row['contact_email'],
            "product_service": row['product_service'],
            "target_audience": row['target_audience'],
            "notification_phone": row['notification_phone'],
            "prospecting_region": row['prospecting_region'],
            "created_at": row['created_at'].isoformat() if row['created_at'] else None,
            "updated_at": row['updated_at'].isoformat() if row['updated_at'] else None
        }


# ==============================================================================
# MEMÓRIA DA IA
# ==============================================================================

@router.get("/memory/{instance_id}")
async def get_ai_memory(instance_id: str, limit: int = 50, admin: Dict = Depends(get_current_admin)):
    """
    Busca a memória da IA de uma instância específica.
    Retorna as últimas N mensagens do contexto.
    """
    
    with get_pool().connection() as conn:
        rows = conn.execute("""
            SELECT 
                id,
                instance_id,
                role,
                content,
                timestamp,
                metadata
            FROM ai_memory
            WHERE instance_id = %s
            ORDER BY timestamp DESC
            LIMIT %s
        """, (instance_id, limit)).fetchall()
        
        return {
            "instance_id": instance_id,
            "total_messages": len(rows),
            "messages": [
                {
                    "id": row['id'],
                    "role": row['role'],
                    "content": row['content'],
                    "timestamp": row['timestamp'].isoformat() if row['timestamp'] else None,
                    "metadata": row['metadata']
                }
                for row in reversed(rows)  # Inverter para ordem cronológica
            ]
        }


@router.get("/memory/{instance_id}/stats")
async def get_ai_memory_stats(instance_id: str, admin: Dict = Depends(get_current_admin)):
    """
    Estatísticas da memória de uma instância.
    """
    
    with get_pool().connection() as conn:
        stats = conn.execute("""
            SELECT 
                COUNT(*) as total_messages,
                COUNT(*) FILTER (WHERE role = 'user') as user_messages,
                COUNT(*) FILTER (WHERE role = 'assistant') as assistant_messages,
                MIN(timestamp) as first_message,
                MAX(timestamp) as last_message
            FROM ai_memory
            WHERE instance_id = %s
        """, (instance_id,)).fetchone()
        
        return {
            "instance_id": instance_id,
            "total_messages": stats['total_messages'] or 0,
            "user_messages": stats['user_messages'] or 0,
            "assistant_messages": stats['assistant_messages'] or 0,
            "first_message": stats['first_message'].isoformat() if stats['first_message'] else None,
            "last_message": stats['last_message'].isoformat() if stats['last_message'] else None
        }


@router.delete("/memory/{instance_id}")
async def reset_ai_memory(instance_id: str, admin: Dict = Depends(get_current_admin)):
    """
    Reseta (apaga) a memória de uma instância específica.
    NÃO apaga toda a tabela, apenas as mensagens desta instância.
    """
    
    admin_id = admin.get("sub", "").split(":")[-1]
    
    with get_pool().connection() as conn:
        # Verificar se instância existe
        instance = conn.execute("""
            SELECT id, instance_id 
            FROM instances 
            WHERE id = %s OR instance_id = %s
        """, (instance_id, instance_id)).fetchone()
        
        if not instance:
            raise HTTPException(status_code=404, detail="Instância não encontrada")
        
        # Contar mensagens antes de deletar
        count = conn.execute("""
            SELECT COUNT(*) as count 
            FROM ai_memory 
            WHERE instance_id = %s
        """, (instance_id,)).fetchone()
        
        messages_deleted = count['count'] or 0
        
        # Deletar memória desta instância
        conn.execute("""
            DELETE FROM ai_memory 
            WHERE instance_id = %s
        """, (instance_id,))
        
        # Registrar ação no log de admin
        conn.execute("""
            INSERT INTO admin_actions (admin_id, action_type, description)
            VALUES (%s, %s, %s)
        """, (
            admin_id,
            'reset_memory',
            f"Memória resetada para instância {instance_id} ({messages_deleted} mensagens deletadas)"
        ))
        
        conn.commit()
        
        log.info(f"[ADMIN] Memória da instância {instance_id} resetada ({messages_deleted} mensagens)")
        
        return {
            "ok": True,
            "instance_id": instance_id,
            "messages_deleted": messages_deleted,
            "message": f"Memória resetada com sucesso. {messages_deleted} mensagens deletadas."
        }


# ==============================================================================
# CONVERSAS (WHATSAPP MONITORING)
# ==============================================================================

@router.post("/instances/{instance_id}/chats")
async def get_instance_chats(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """
    Lista todas as conversas (chats) de uma instância do WhatsApp.
    Retorna informações dos chats armazenados localmente no banco.
    """

    log.info(f"[CONVERSAS] 🔵 GET /instances/{instance_id}/chats - Admin: {admin.get('email')}")

    try:
        with get_pool().connection() as conn:
            log.info(f"[CONVERSAS] ✅ Conexão com banco OK")

            _ensure_instance_exists(conn, instance_id)
            log.info(f"[CONVERSAS] ✅ Instância {instance_id} existe")

            with conn.cursor() as cur:
                # Buscar chats distintos com última mensagem
                log.info(f"[CONVERSAS] 🔍 Executando query SQL...")
                cur.execute("""
                    SELECT
                        m.chat_id,
                        MAX(m.timestamp) as last_timestamp,
                        (
                            SELECT content
                            FROM messages m2
                            WHERE m2.chat_id = m.chat_id
                            AND m2.instance_id = m.instance_id
                            ORDER BY m2.timestamp DESC
                            LIMIT 1
                        ) as last_message,
                        (
                            SELECT from_me
                            FROM messages m2
                            WHERE m2.chat_id = m.chat_id
                            AND m2.instance_id = m.instance_id
                            ORDER BY m2.timestamp DESC
                            LIMIT 1
                        ) as last_from_me,
                        COUNT(*) as message_count
                    FROM messages m
                    WHERE m.instance_id = %s
                    GROUP BY m.chat_id, m.instance_id
                    ORDER BY MAX(m.timestamp) DESC
                    LIMIT 100
                """, (instance_id,))

                rows = cur.fetchall()
                log.info(f"[CONVERSAS] ✅ Query executada. Rows: {len(rows)}")

                chats = []
                for i, row in enumerate(rows):
                    log.debug(f"[CONVERSAS] Processando row {i+1}/{len(rows)}: {row}")

                    # Extract phone number from chat_id (format: "5511999998888@c.us")
                    # IMPORTANTE: row é um DICT, não tupla (por causa do row_factory=dict_row)
                    chat_id = row["chat_id"]
                    phone = chat_id.split('@')[0] if '@' in chat_id else chat_id

                    # Formatar nome amigável para exibição
                    def format_phone_display(phone_num):
                        """Formata número de telefone para exibição amigável"""
                        # Remove caracteres não numéricos
                        cleaned = ''.join(filter(str.isdigit, phone_num))

                        # Formato brasileiro: +55 (XX) XXXXX-XXXX
                        if len(cleaned) >= 12:
                            country = cleaned[:2]
                            ddd = cleaned[2:4]
                            if len(cleaned) == 13:  # Com 9 dígitos
                                number = f"{cleaned[4:9]}-{cleaned[9:13]}"
                            elif len(cleaned) == 12:  # 8 dígitos
                                number = f"{cleaned[4:8]}-{cleaned[8:12]}"
                            else:
                                number = cleaned[4:]
                            return f"+{country} ({ddd}) {number}"

                        # Fallback: retorna "Cliente" + número limpo
                        return f"Cliente {phone_num}"

                    name = format_phone_display(phone)

                    chats.append({
                        "_chatId": chat_id,
                        "lead_name": name,
                        "wa_lastMessageTextVote": row.get("last_message") or "",
                        "wa_lastMsgPreview": (row.get("last_message") or "")[:100],
                        "phone": phone,
                        "last_timestamp": int(row["last_timestamp"]) if row.get("last_timestamp") else 0,
                        "last_from_me": bool(row.get("last_from_me", False)),
                        "message_count": row.get("message_count", 0)
                    })

                log.info(f"[CONVERSAS] ✅ {len(chats)} chats processados com sucesso")
                return {"items": chats, "total": len(chats)}

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[CONVERSAS] ❌ Erro inesperado: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao buscar conversas: {str(e)}")


@router.post("/instances/{instance_id}/messages")
async def get_instance_messages(
    instance_id: str,
    body: Dict = None,
    admin: Dict = Depends(get_current_admin)
):
    """
    Retorna as mensagens de um chat específico.
    Body deve conter: { "chatId": "5511999998888@c.us" }
    """

    if not body or "chatId" not in body:
        raise HTTPException(status_code=400, detail="chatId é obrigatório")

    chat_id = body.get("chatId")

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    content,
                    from_me,
                    timestamp,
                    media_type,
                    media_url
                FROM messages
                WHERE instance_id = %s AND chat_id = %s
                ORDER BY timestamp ASC
                LIMIT 500
            """, (instance_id, chat_id))

            rows = cur.fetchall()

            messages = []
            for row in rows:
                # IMPORTANTE: row é um DICT (row_factory=dict_row)
                from_me_val = row["from_me"]  # ✅ Acesso direto
                content_val = row["content"]
                timestamp_val = row["timestamp"]
                media_type_val = row["media_type"]
                media_url_val = row["media_url"]

                msg_obj = {
                    "text": content_val or "",
                    "fromMe": bool(from_me_val),  # ✅ Conversão snake_case → camelCase
                    "messageTimestamp": int(timestamp_val) if timestamp_val else 0,
                    "type": media_type_val or "text",
                    "mediaUrl": media_url_val
                }
                messages.append(msg_obj)

                # 🔍 LOG DEBUG
                log.info(f"[ADMIN] ✅ from_me={from_me_val} → fromMe={msg_obj['fromMe']} | texto={(content_val or '')[:30]}")

            log.info(f"[ADMIN] 📤 Retornando {len(messages)} mensagens para chat {chat_id}")
            return {"items": messages, "total": len(messages)}


@router.post("/instances/{instance_id}/export-analysis")
async def export_chat_analysis(
    instance_id: str,
    body: Dict = None,
    admin: Dict = Depends(get_current_admin)
):
    """
    Gera análise de IA de uma conversa e retorna como texto/JSON.
    Body deve conter: { "chatId": "5511999998888@c.us", "leadName": "João Silva" }
    """

    if not body or "chatId" not in body:
        raise HTTPException(status_code=400, detail="chatId é obrigatório")

    chat_id = body.get("chatId")
    lead_name = body.get("leadName", "Cliente")

    try:
        # Buscar mensagens da conversa
        with get_pool().connection() as conn:
            _ensure_instance_exists(conn, instance_id)

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        content,
                        from_me,
                        timestamp
                    FROM messages
                    WHERE instance_id = %s AND chat_id = %s
                    ORDER BY timestamp ASC
                    LIMIT 500
                """, (instance_id, chat_id))

                rows = cur.fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail="Nenhuma mensagem encontrada para este chat")

        # Formatar mensagens para análise
        conversation_text = ""
        for row in rows:
            # IMPORTANTE: row é um DICT, não tupla (por causa do row_factory=dict_row)
            sender = "Atendente" if row.get("from_me") else lead_name
            message = row.get("content") or ""
            timestamp_val = row.get("timestamp")
            if timestamp_val:
                # timestamp pode estar em millisegundos
                ts = int(timestamp_val)
                if ts > 10000000000:  # Está em millisegundos
                    ts = ts // 1000
                timestamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            else:
                timestamp = ""
            conversation_text += f"[{timestamp}] {sender}: {message}\n"

        # Gerar análise com OpenAI
        import openai
        openai_key = os.getenv("OPENAI_API_KEY")

        if not openai_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY não configurada")

        client = openai.OpenAI(api_key=openai_key)

        prompt = f"""Analise a seguinte conversa de WhatsApp entre um atendente e o cliente {lead_name}.

Forneça uma análise detalhada incluindo:
1. Resumo da conversa
2. Principais pontos discutidos
3. Interesse do cliente (alto/médio/baixo)
4. Próximos passos sugeridos
5. Observações importantes

Conversa:
{conversation_text}
"""

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "Você é um analista de vendas especializado em analisar conversas de WhatsApp."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_completion_tokens=4000  # ✅ GPT-5 mini precisa de mais tokens (usa reasoning_tokens internos)
        )

        analysis = response.choices[0].message.content

        # Retornar análise como JSON (frontend pode converter para PDF)
        return {
            "ok": True,
            "chatId": chat_id,
            "leadName": lead_name,
            "analysis": analysis,
            "messageCount": len(rows),
            "generatedAt": datetime.now(timezone.utc).isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Erro ao gerar análise: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro ao gerar análise: {str(e)}")


# =========================================
# AUTOMAÇÃO - EXECUÇÃO DO LOOP
# =========================================

# Controle de loops em execução
running_automations = {}  # {instance_id: {"task": asyncio.Task, "stop_requested": bool}}

@router.get("/instances/{instance_id}/automation-state")
async def get_automation_state(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """Verificar estado atual da automação"""

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

        with conn.cursor() as cur:
            # Buscar configurações
            cur.execute("""
                SELECT daily_limit, auto_run, ia_auto
                FROM instance_settings
                WHERE instance_id = %s
            """, (instance_id,))

            settings_row = cur.fetchone()
            if not settings_row:
                daily_limit = 30
            else:
                daily_limit = settings_row['daily_limit']

            # Contar enviados hoje
            cur.execute("""
                SELECT COUNT(*) as count
                FROM instance_totals
                WHERE instance_id = %s
                  AND mensagem_enviada = true
                  AND updated_at::date = CURRENT_DATE
            """, (instance_id,))

            sent_today = cur.fetchone()['count']
            remaining_today = max(0, daily_limit - sent_today)

            # Verificar se está rodando
            is_running = instance_id in running_automations

            response = {
                "loop_status": "running" if is_running else "idle",
                "sent_today": sent_today,
                "cap": daily_limit,
                "remaining_today": remaining_today,
                "actually_running": is_running,
                "now": datetime.now(timezone.utc).isoformat()
            }

            # Se estiver rodando, adicionar informações de timing
            if is_running and instance_id in running_automations:
                automation_info = running_automations[instance_id]
                response["last_sent_at"] = automation_info.get("last_sent_at")
                response["next_message_at"] = automation_info.get("next_message_at")
                response["average_interval_seconds"] = automation_info.get("average_interval_seconds")

            return response


@router.post("/instances/{instance_id}/run-automation")
async def run_automation(
    instance_id: str,
    background_tasks: BackgroundTasks,
    admin: Dict = Depends(get_current_admin)
):
    """Executar loop de automação"""

    if instance_id in running_automations:
        raise HTTPException(status_code=409, detail="Automação já está em execução")

    with get_pool().connection() as conn:
        _ensure_instance_exists(conn, instance_id)

    # Iniciar automação em background
    background_tasks.add_task(_run_automation_loop, instance_id)

    return {"ok": True, "message": "Automação iniciada"}


@router.post("/instances/{instance_id}/stop-automation")
async def stop_automation(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """Parar loop de automação em execução"""
    log.info(f"🛑 [AUTOMATION] Solicitação de parada recebida para {instance_id}")
    log.info(f"🛑 [AUTOMATION] Automações em execução: {list(running_automations.keys())}")

    if instance_id not in running_automations:
        log.warning(f"⚠️ [AUTOMATION] Instância {instance_id} não está em execução")
        raise HTTPException(status_code=404, detail="Nenhuma automação em execução")

    # Marcar para parar
    running_automations[instance_id]["stop_requested"] = True
    log.info(f"✅ [AUTOMATION] Flag de parada definida para {instance_id}")

    return {"ok": True, "message": "Parada solicitada"}


# =========================================
# LÓGICA DE AUTOMAÇÃO
# =========================================

async def _run_automation_loop(instance_id: str):
    """Loop principal de automação"""
    log.info(f"🤖 [AUTOMATION] Iniciando loop para instância {instance_id}")

    # Registrar que está rodando
    running_automations[instance_id] = {
        "task": asyncio.current_task(),
        "stop_requested": False,
        "last_sent_at": None,
        "next_message_at": None,
        "average_interval_seconds": None
    }

    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Buscar configurações
                cur.execute("""
                    SELECT
                        s.daily_limit,
                        s.ia_auto,
                        s.message_template,
                        i.uazapi_host,
                        i.uazapi_token
                    FROM instance_settings s
                    JOIN instances i ON i.id = s.instance_id
                    WHERE s.instance_id = %s
                """, (instance_id,))

                settings = cur.fetchone()
                if not settings:
                    log.error(f"❌ [AUTOMATION] Configurações não encontradas para {instance_id}")
                    return

                daily_limit = settings['daily_limit']
                ia_auto = settings['ia_auto']
                message_template = settings['message_template'] or "Olá {nome}! Tudo bem?"
                instance_url = settings['uazapi_host']
                instance_token = settings['uazapi_token']

                if not instance_url or not instance_token:
                    log.error(f"❌ [AUTOMATION] uazapi_host ou uazapi_token não configurados")
                    return

                # Contar já enviados hoje
                cur.execute("""
                    SELECT COUNT(*) as count
                    FROM instance_totals
                    WHERE instance_id = %s
                      AND mensagem_enviada = true
                      AND updated_at::date = CURRENT_DATE
                """, (instance_id,))

                sent_today = cur.fetchone()['count']
                remaining = max(0, daily_limit - sent_today)

                log.info(f"📊 [AUTOMATION] Enviados hoje: {sent_today}/{daily_limit}, restantes: {remaining}")

                if remaining <= 0:
                    log.info(f"✅ [AUTOMATION] Limite diário atingido")
                    return

                # Calcular distribuição de horários (7:30 - 17:30) - HORÁRIO DE BRASÍLIA
                from datetime import datetime, time, timedelta, timezone
                import random

                # Usar horário de Brasília (UTC-3)
                TZ_BRASILIA = timezone(timedelta(hours=-3))
                agora = datetime.now(TZ_BRASILIA)
                hora_inicio = agora.replace(hour=7, minute=30, second=0, microsecond=0)
                hora_fim = agora.replace(hour=17, minute=30, second=0, microsecond=0)

                log.info(f"⏰ [AUTOMATION] Horário atual (Brasília): {agora.strftime('%H:%M:%S')}")

                # Se for antes das 7:30, esperar até 7:30
                if agora < hora_inicio:
                    wait_seconds = (hora_inicio - agora).total_seconds()
                    log.info(f"⏰ [AUTOMATION] Antes do horário permitido. Aguardando até 07:30 ({wait_seconds/60:.1f} minutos)...")
                    await asyncio.sleep(wait_seconds)
                    agora = datetime.now(TZ_BRASILIA)

                # Se for depois das 17:30, não enviar
                if agora > hora_fim:
                    log.info(f"⏰ [AUTOMATION] Fora do horário permitido (07:30-17:30). Finalizando.")
                    return

                # Calcular tempo disponível e intervalo entre mensagens
                tempo_disponivel = (hora_fim - agora).total_seconds()  # em segundos
                intervalo_medio = tempo_disponivel / remaining if remaining > 0 else 60

                log.info(f"⏱️ [AUTOMATION] Distribuindo {remaining} mensagens em {tempo_disponivel/3600:.1f} horas")
                log.info(f"⏱️ [AUTOMATION] Intervalo médio: {intervalo_medio/60:.1f} minutos por mensagem")

                # Buscar contatos da fila
                processed = 0

                for i in range(remaining):
                    # Verificar se foi solicitado parar
                    if instance_id in running_automations and running_automations[instance_id].get("stop_requested", False):
                        log.info(f"⏹️ [AUTOMATION] Parada solicitada após {processed} envios")
                        break

                    # Verificar se ainda está no horário permitido
                    agora_check = datetime.now(TZ_BRASILIA)
                    if agora_check > hora_fim:
                        log.info(f"⏰ [AUTOMATION] Fim do horário permitido (17:30). Processados: {processed}")
                        break

                    # Buscar próximo contato da fila que NÃO foi enviado
                    cur.execute("""
                        SELECT q.name, q.phone, q.niche
                        FROM instance_queue q
                        LEFT JOIN instance_totals t ON t.instance_id = q.instance_id AND t.phone = q.phone
                        WHERE q.instance_id = %s
                          AND (t.mensagem_enviada IS NOT TRUE OR t.phone IS NULL)
                        ORDER BY q.created_at ASC
                        LIMIT 1
                    """, (instance_id,))

                    contact = cur.fetchone()

                    if not contact:
                        log.info(f"✅ [AUTOMATION] Fila vazia, finalizando")
                        break

                    name = contact['name']
                    phone = contact['phone']
                    niche = contact['niche'] or ''

                    log.info(f"📤 [AUTOMATION] Enviando para {name} ({phone})")

                    # Determinar saudação baseada no horário (usar horário de Brasília)
                    hora_atual = datetime.now(TZ_BRASILIA).hour
                    if 5 <= hora_atual < 12:
                        saudacao = "Bom dia"
                    elif 12 <= hora_atual < 18:
                        saudacao = "Boa tarde"
                    else:
                        saudacao = "Boa noite"

                    # Preparar mensagem
                    message = message_template.replace('{nome}', name).replace('{phone}', phone).replace('{niche}', niche).replace('{saudacao}', saudacao)

                    # Enviar mensagem via UAZAPI
                    success = await _send_whatsapp_message(instance_url, instance_token, phone, message)

                    if success:
                        log.info(f"✅ [AUTOMATION] Mensagem enviada com sucesso para {phone}")

                        # Marcar como enviado em instance_totals
                        cur.execute("""
                            INSERT INTO instance_totals (instance_id, name, phone, niche, mensagem_enviada, updated_at)
                            VALUES (%s, %s, %s, %s, true, NOW())
                            ON CONFLICT (instance_id, phone)
                            DO UPDATE SET
                                mensagem_enviada = true,
                                updated_at = NOW()
                        """, (instance_id, name, phone, niche))

                        processed += 1
                    else:
                        log.warning(f"⚠️ [AUTOMATION] Falha ao enviar para {phone}")

                    # Remover da fila SEMPRE (mesmo se falhou)
                    cur.execute("""
                        DELETE FROM instance_queue
                        WHERE instance_id = %s AND phone = %s
                    """, (instance_id, phone))

                    conn.commit()

                    # Delay inteligente com distribuição ao longo do dia
                    # Adiciona aleatoriedade de ±30% para parecer mais natural
                    variacao = random.uniform(0.7, 1.3)
                    delay = int(intervalo_medio * variacao)

                    # Garantir mínimo de 30 segundos e máximo de 2 horas
                    delay = max(30, min(delay, 7200))

                    # Atualizar informações de timing no running_automations
                    if instance_id in running_automations:
                        agora_brasilia = datetime.now(TZ_BRASILIA)
                        proxima_msg = agora_brasilia + timedelta(seconds=delay)

                        running_automations[instance_id]["last_sent_at"] = agora_brasilia.isoformat()
                        running_automations[instance_id]["next_message_at"] = proxima_msg.isoformat()
                        running_automations[instance_id]["average_interval_seconds"] = int(intervalo_medio)

                    log.info(f"⏱️ [AUTOMATION] Aguardando {delay}s ({delay/60:.1f} min) antes da próxima mensagem...")
                    log.info(f"⏱️ [AUTOMATION] Progresso: {processed}/{remaining} enviados")

                    await asyncio.sleep(delay)

                log.info(f"✅ [AUTOMATION] Loop finalizado. Processados: {processed}")

    except Exception as e:
        log.error(f"❌ [AUTOMATION] Erro no loop: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Remover do registro
        if instance_id in running_automations:
            del running_automations[instance_id]
            log.info(f"🏁 [AUTOMATION] Instância {instance_id} removida do registro de execução")


async def _send_whatsapp_message(instance_url: str, instance_token: str, phone: str, message: str) -> bool:
    """Enviar mensagem via UAZAPI"""
    try:
        # Normalizar número
        clean_phone = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        if not clean_phone.startswith('55'):
            clean_phone = '55' + clean_phone

        # Normalizar URL (garantir protocolo)
        if not instance_url.startswith('http://') and not instance_url.startswith('https://'):
            instance_url = 'https://' + instance_url

        # Montar URL e headers
        url = instance_url.rstrip('/') + '/send/text'

        headers = {
            'Content-Type': 'application/json',
            'token': instance_token
        }

        payload = {
            'number': clean_phone,
            'text': message,
            'delay': 3000  # 3 segundos de delay para simular digitação
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                return True
            else:
                log.error(f"❌ [UAZAPI] Erro {response.status_code}: {response.text}")
                return False

    except Exception as e:
        log.error(f"❌ [UAZAPI] Exceção ao enviar: {e}")
        return False


# ==============================================================================
# AGENDADOR AUTOMÁTICO - INICIA LOOP DIARIAMENTE ÀS 7:30 (SEG-SEX)
# ==============================================================================

scheduler_task = None

async def automation_scheduler():
    """
    Scheduler que roda em background e inicia automaticamente
    o loop de automação todos os dias às 7:30 (segunda a sexta)
    HORÁRIO DE BRASÍLIA (UTC-3)
    """
    log.info("📅 [SCHEDULER] Agendador de automação iniciado (Horário de Brasília UTC-3)")

    ultima_execucao = None

    while True:
        try:
            # Usar horário de Brasília (UTC-3)
            from datetime import timezone, timedelta
            TZ_BRASILIA = timezone(timedelta(hours=-3))
            agora = datetime.now(TZ_BRASILIA)

            dia_semana = agora.weekday()  # 0=segunda, 6=domingo
            hora_atual = agora.hour
            minuto_atual = agora.minute

            # Verificar se é dia útil (segunda=0 a sexta=4)
            eh_dia_util = dia_semana < 5

            # Verificar se é 7:30 (horário de Brasília)
            eh_horario_inicio = hora_atual == 7 and minuto_atual == 30

            # Chave única para evitar execução duplicada no mesmo minuto
            chave_execucao = f"{agora.date()}-{hora_atual}:{minuto_atual}"

            if eh_dia_util and eh_horario_inicio and chave_execucao != ultima_execucao:
                log.info(f"🚀 [SCHEDULER] Horário de início detectado: {agora.strftime('%d/%m/%Y %H:%M')}")
                ultima_execucao = chave_execucao

                # Buscar todas as instâncias com auto_run=true
                with get_pool().connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT i.id, i.instance_id, u.email
                            FROM instances i
                            JOIN instance_settings s ON s.instance_id = i.id
                            JOIN users u ON u.id = i.user_id
                            WHERE s.auto_run = true
                        """)

                        instances = cur.fetchall()

                        if instances:
                            log.info(f"📊 [SCHEDULER] Encontradas {len(instances)} instâncias com auto_run ativo")

                            for instance in instances:
                                instance_id = instance['id']
                                email = instance['email']

                                # Verificar se já não está rodando
                                if instance_id not in running_automations:
                                    log.info(f"▶️ [SCHEDULER] Iniciando automação para {email} ({instance_id})")

                                    # Iniciar em background (sem await para não bloquear)
                                    asyncio.create_task(_run_automation_loop(instance_id))
                                else:
                                    log.info(f"⏭️ [SCHEDULER] Automação já rodando para {instance_id}")
                        else:
                            log.info("ℹ️ [SCHEDULER] Nenhuma instância com auto_run ativo encontrada")

            # Aguardar 60 segundos antes de verificar novamente
            await asyncio.sleep(60)

        except Exception as e:
            log.error(f"❌ [SCHEDULER] Erro no scheduler: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(60)  # Aguardar 1 minuto antes de tentar novamente


def start_scheduler():
    """Inicia o scheduler em background"""
    global scheduler_task

    if scheduler_task is None:
        scheduler_task = asyncio.create_task(automation_scheduler())
        log.info("✅ [SCHEDULER] Task de agendamento criada")
    else:
        log.info("ℹ️ [SCHEDULER] Scheduler já está rodando")


@router.get("/instances/{instance_id}/next-run")
async def get_next_run(
    instance_id: str,
    admin: Dict = Depends(get_current_admin)
):
    """Retorna informações sobre a próxima execução automática"""

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Buscar configuração
            cur.execute("""
                SELECT auto_run, daily_limit
                FROM instance_settings
                WHERE instance_id = %s
            """, (instance_id,))

            settings = cur.fetchone()

            if not settings:
                raise HTTPException(status_code=404, detail="Configurações não encontradas")

            auto_run = settings['auto_run']
            daily_limit = settings['daily_limit']

            # Calcular próxima execução
            agora = datetime.now()
            dia_semana = agora.weekday()  # 0=segunda, 6=domingo

            # Calcular próximo dia útil às 7:30
            if dia_semana < 5:  # Segunda a sexta
                # Se for antes das 7:30 hoje, próxima execução é hoje
                if agora.hour < 7 or (agora.hour == 7 and agora.minute < 30):
                    proxima_execucao = agora.replace(hour=7, minute=30, second=0, microsecond=0)
                else:
                    # Senão, é amanhã (se for sexta, pula para segunda)
                    dias_ate_proximo = 3 if dia_semana == 4 else 1  # sexta -> segunda (3 dias)
                    proxima_execucao = (agora + timedelta(days=dias_ate_proximo)).replace(hour=7, minute=30, second=0, microsecond=0)
            else:
                # Final de semana -> próxima segunda 7:30
                dias_ate_segunda = (7 - dia_semana) % 7
                if dias_ate_segunda == 0:
                    dias_ate_segunda = 1  # Se for domingo, próxima segunda é em 1 dia
                proxima_execucao = (agora + timedelta(days=dias_ate_segunda)).replace(hour=7, minute=30, second=0, microsecond=0)

            # Verificar se está rodando agora
            is_running = instance_id in running_automations

            # Tempo até próxima execução
            tempo_ate_proxima = (proxima_execucao - agora).total_seconds()

            return {
                "auto_run": auto_run,
                "daily_limit": daily_limit,
                "is_running": is_running,
                "next_run": proxima_execucao.isoformat(),
                "next_run_formatted": proxima_execucao.strftime("%d/%m/%Y às %H:%M"),
                "next_run_day_name": ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"][proxima_execucao.weekday()],
                "seconds_until_next": int(tempo_ate_proxima),
                "hours_until_next": round(tempo_ate_proxima / 3600, 1),
                "current_time": agora.isoformat()
            }
