"""
Rotas para gerenciar questionários de onboarding dos usuários
"""
from __future__ import annotations
from typing import Dict, Any, Optional
from datetime import datetime
import logging

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, EmailStr

from app.pg import get_pool
from app.routes.deps import get_current_user

router = APIRouter()
log = logging.getLogger("uvicorn.error")

# ==============================================================================
# MODELOS
# ==============================================================================

class QuestionnaireSubmit(BaseModel):
    """Dados do questionário enviados pelo usuário"""
    has_whatsapp_number: bool
    company_name: str
    contact_phone: str
    contact_email: EmailStr
    product_service: str
    target_audience: str
    notification_phone: str
    prospecting_region: str

class QuestionnaireResponse(BaseModel):
    """Resposta com dados do questionário"""
    id: int
    user_id: int
    has_whatsapp_number: bool
    company_name: str
    contact_phone: str
    contact_email: str
    product_service: str
    target_audience: str
    notification_phone: str
    prospecting_region: str
    created_at: datetime
    updated_at: datetime

# ==============================================================================
# ROTAS
# ==============================================================================

@router.post("/submit")
async def submit_questionnaire(
    questionnaire: QuestionnaireSubmit,
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Salva ou atualiza o questionário do usuário.
    
    Fluxo:
    1. Valida se usuário está autenticado
    2. Salva respostas no banco (INSERT ou UPDATE se já existir)
    3. Retorna confirmação
    """
    user_id = user["id"]
    
    log.info(f"📝 [QUESTIONNAIRE] Salvando questionário para user_id={user_id}")
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Verificar se já existe questionário para este usuário
                cur.execute(
                    "SELECT id FROM user_questionnaires WHERE user_id = %s",
                    (user_id,)
                )
                existing = cur.fetchone()
                
                if existing:
                    # Atualizar questionário existente
                    cur.execute(
                        """
                        UPDATE user_questionnaires
                        SET has_whatsapp_number = %s,
                            company_name = %s,
                            contact_phone = %s,
                            contact_email = %s,
                            product_service = %s,
                            target_audience = %s,
                            notification_phone = %s,
                            prospecting_region = %s,
                            updated_at = NOW()
                        WHERE user_id = %s
                        RETURNING id
                        """,
                        (
                            questionnaire.has_whatsapp_number,
                            questionnaire.company_name,
                            questionnaire.contact_phone,
                            questionnaire.contact_email,
                            questionnaire.product_service,
                            questionnaire.target_audience,
                            questionnaire.notification_phone,
                            questionnaire.prospecting_region,
                            user_id
                        )
                    )
                    log.info(f"✅ [QUESTIONNAIRE] Questionário atualizado para user_id={user_id}")
                else:
                    # Criar novo questionário
                    cur.execute(
                        """
                        INSERT INTO user_questionnaires (
                            user_id, has_whatsapp_number, company_name,
                            contact_phone, contact_email, product_service,
                            target_audience, notification_phone, prospecting_region
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            user_id,
                            questionnaire.has_whatsapp_number,
                            questionnaire.company_name,
                            questionnaire.contact_phone,
                            questionnaire.contact_email,
                            questionnaire.product_service,
                            questionnaire.target_audience,
                            questionnaire.notification_phone,
                            questionnaire.prospecting_region
                        )
                    )
                    log.info(f"✅ [QUESTIONNAIRE] Questionário criado para user_id={user_id}")
                
                conn.commit()
                
                return {
                    "ok": True,
                    "message": "Questionário salvo com sucesso!",
                    "user_id": user_id
                }
                
    except Exception as e:
        log.error(f"❌ [QUESTIONNAIRE] Erro ao salvar questionário: {e}")
        raise HTTPException(500, f"Erro ao salvar questionário: {str(e)}")


@router.get("/status")
async def check_questionnaire_status(user: Dict[str, Any] = Depends(get_current_user)):
    """
    Verifica se o usuário já preencheu o questionário.
    
    Retorna:
    - completed: boolean indicando se já preencheu
    - questionnaire: dados do questionário (se existir)
    """
    user_id = user["id"]
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM user_questionnaires
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )
                questionnaire = cur.fetchone()
                
                if questionnaire:
                    return {
                        "completed": True,
                        "questionnaire": dict(questionnaire)
                    }
                else:
                    return {
                        "completed": False,
                        "questionnaire": None
                    }
                    
    except Exception as e:
        log.error(f"❌ [QUESTIONNAIRE] Erro ao verificar status: {e}")
        raise HTTPException(500, f"Erro ao verificar questionário: {str(e)}")


@router.get("/get")
async def get_questionnaire(user: Dict[str, Any] = Depends(get_current_user)):
    """
    Busca o questionário do usuário autenticado.
    """
    user_id = user["id"]
    
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM user_questionnaires
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )
                questionnaire = cur.fetchone()
                
                if not questionnaire:
                    raise HTTPException(404, "Questionário não encontrado")
                
                return dict(questionnaire)
                
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"❌ [QUESTIONNAIRE] Erro ao buscar questionário: {e}")
        raise HTTPException(500, f"Erro ao buscar questionário: {str(e)}")
