# ROTAS ADMINISTRATIVAS - PAINEL ADMIN
from __future__ import annotations
from typing import Dict, Any, List
from datetime import datetime, timedelta, timezone
import logging
import os
import jwt
import bcrypt

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
    email: EmailStr
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
    
    admin_id = int(admin['sub'].split(':')[1])
    
    with get_pool().connection() as conn:
        # Buscar instância
        instance = conn.execute(
            "SELECT * FROM instances WHERE id = %s",
            (instance_id,)
        ).fetchone()
        
        if not instance:
            raise HTTPException(status_code=404, detail="Instância não encontrada")
        
        # Salvar prompt anterior no histórico
        old_prompt = instance['prompt']
        prompt_history = instance['prompt_history'] or []
        if old_prompt:
            prompt_history.append({
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "changed_by": admin_id,
                "old_prompt": old_prompt,
                "new_prompt": body.prompt
            })
        
        # Atualizar instância
        conn.execute("""
            UPDATE instances
            SET 
                admin_status = 'configured',
                configured_by = %s,
                configured_at = NOW(),
                prompt = %s,
                admin_notes = %s,
                prompt_history = %s,
                redirect_phone = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (admin_id, body.prompt, body.notes, prompt_history, body.redirect_phone, instance_id))
        
        # Registrar ação
        conn.execute("""
            INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description)
            VALUES (%s, 'configure_instance', 'instance', %s, 'Prompt configurado e instância ativada')
        """, (admin_id, instance_id))
        
        # Notificar usuário
        conn.execute("""
            INSERT INTO notifications (recipient_type, recipient_id, type, title, message)
            VALUES ('user', %s, 'instance_configured', 'Sua Luna está ativa!',
                    'Sua Luna foi configurada pela equipe Helsen e já está operacional!')
        """, (instance['user_id'],))
        
        return {"ok": True, "message": "Instância configurada com sucesso"}

@router.post("/instances/{instance_id}/suspend")
async def suspend_instance(
    instance_id: str,
    reason: str,
    admin: Dict = Depends(get_current_admin)
):
    """Suspender uma instância"""
    
    admin_id = int(admin['sub'].split(':')[1])
    
    with get_pool().connection() as conn:
        instance = conn.execute(
            "SELECT * FROM instances WHERE id = %s",
            (instance_id,)
        ).fetchone()
        
        if not instance:
            raise HTTPException(status_code=404, detail="Instância não encontrada")
        
        # Suspender
        conn.execute("""
            UPDATE instances
            SET 
                admin_status = 'suspended',
                admin_notes = COALESCE(admin_notes || E'\\n', '') || '[' || NOW() || '] Suspensa: ' || %s,
                updated_at = NOW()
            WHERE id = %s
        """, (reason, instance_id))
        
        # Registrar ação
        conn.execute("""
            INSERT INTO admin_actions (admin_id, action_type, target_type, target_id, description)
            VALUES (%s, 'suspend_instance', 'instance', %s, %s)
        """, (admin_id, instance_id, f'Instância suspensa: {reason}'))
        
        return {"ok": True, "message": "Instância suspensa"}

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
