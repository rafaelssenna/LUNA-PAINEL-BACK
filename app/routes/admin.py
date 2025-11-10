# ROTAS ADMINISTRATIVAS - PAINEL ADMIN
from __future__ import annotations
from typing import Dict, Any, List
from datetime import datetime, timedelta, timezone
import logging
import os
import jwt
import bcrypt
import json

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr

from app.pg import get_pool

router = APIRouter()
log = logging.getLogger("uvicorn.error")

JWT_SECRET = os.getenv("LUNA_JWT_SECRET") or os.getenv("JWT_SECRET") or "change-me"
JWT_ALG = os.getenv("JWT_ALGORITHM", "HS256")
JWT_TTL_SECONDS = 86400  # 24 horas para admin

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
    """Instâncias aguardando configuração"""
    
    with get_pool().connection() as conn:
        rows = conn.execute("SELECT * FROM get_pending_instances()").fetchall()
        
        return [
            {
                "instance_uuid": str(row['instance_uuid']),
                "instance_id": row['instance_id'],
                "user_email": row['user_email'],
                "user_name": row['user_name'],
                "phone_number": row['phone_number'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                "hours_waiting": float(row['hours_waiting']) if row['hours_waiting'] else 0
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
                
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "phone_number": row[2],
                    "status": row[3],
                    "admin_status": row[4],
                    "prompt": row[5],  # ✅ Prompt completo
                    "redirect_phone": row[6],
                    "admin_notes": row[7],
                    "configured_at": row[8].isoformat() if row[8] else None,
                    "created_at": row[9].isoformat() if row[9] else None,
                    "updated_at": row[10].isoformat() if row[10] else None,
                    "user_email": row[11],
                    "user_name": row[12]
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
