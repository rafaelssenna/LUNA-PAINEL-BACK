# app/routes/webhook.py
"""
WEBHOOK WHATSAPP - Recebe mensagens e responde com IA
Implementação do agente Luna com Function Calling (igual ao TypeScript)
"""
from __future__ import annotations

import os
import logging
import json
import asyncio
import random
from typing import Dict, Any, List, Optional
from datetime import datetime
from collections import defaultdict

import httpx
from fastapi import APIRouter, Request, BackgroundTasks
from openai import AsyncOpenAI

from app.pg import get_pool

router = APIRouter()
log = logging.getLogger("uvicorn.error")

# ==============================================================================
# CONFIGURAÇÕES
# ==============================================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "300"))  # Luna deve ser concisa
MAX_HISTORY = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
BUFFER_SECONDS = 7  # 7 segundos para agrupar mensagens
UAZAPI_HOST = os.getenv("UAZAPI_HOST", "hia-clientes.uazapi.com")
MIN_TYPING_DELAY = 1.5  # segundos
MAX_TYPING_DELAY = 3.5  # segundos
REDIRECT_PHONE = os.getenv("REDIRECT_PHONE", "")  # Fallback global

# Buffer de mensagens (número -> dados pendentes)
pending_messages: Dict[str, Dict[str, Any]] = {}
processing_lock: Dict[str, bool] = defaultdict(bool)

# Cliente OpenAI
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ==============================================================================
# FUNÇÕES AUXILIARES
# ==============================================================================

# ==============================================================================
# FUNÇÕES AUXILIARES
# ==============================================================================
def normalize_number(num: str) -> str:
    """Remove caracteres não numéricos"""
    if not num:
        return ""
    return "".join(c for c in str(num) if c.isdigit()).split("@")[0] if "@" in str(num) else "".join(c for c in str(num) if c.isdigit())


def extract_text(data: Dict[str, Any]) -> str:
    """Extrai texto de payload UAZAPI/WhatsApp"""
    # Tenta vários caminhos possíveis
    paths = [
        ["text"],
        ["message", "conversation"],
        ["message", "extendedTextMessage", "text"],
        ["body"],
        ["caption"],
        ["chat", "text"],
        ["data", "message", "conversation"],
    ]
    
    for path in paths:
        val = data
        for key in path:
            if isinstance(val, dict):
                val = val.get(key)
            else:
                break
        if isinstance(val, str) and val.strip():
            return val.strip()
    
    return ""


def extract_number(data: Dict[str, Any]) -> str:
    """Extrai número do remetente"""
    fields = ["number", "from", "chatid", "chatId", "phone", "sender"]
    
    for field in fields:
        val = data.get(field)
        if val:
            return normalize_number(str(val))
    
    # Tenta dentro de objetos aninhados
    if isinstance(data.get("chat"), dict):
        for field in fields:
            val = data["chat"].get(field)
            if val:
                return normalize_number(str(val))
    
    return ""


async def get_instance_config(instance_id: str) -> Optional[Dict[str, Any]]:
    """Busca configuração da instância no banco"""
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, uazapi_host, uazapi_token, prompt, status, redirect_phone, admin_status
                    FROM instances
                    WHERE id = %s
                    """,
                    (instance_id,)
                )
                row = cur.fetchone()
                if not row:
                    return None
                
                # Se não tem prompt configurado, não processa (admin ainda não configurou)
                if not row[3]:
                    log.warning(f"⚠️ Instância {instance_id} sem prompt configurado")
                    return None
                
                return {
                    "id": row[0],
                    "host": row[1],
                    "token": row[2],
                    "prompt": row[3],  # ✅ Prompt específico da instância (configurado pelo admin)
                    "status": row[4],
                    "redirect_phone": row[5],  # ✅ Número específico da instância
                    "admin_status": row[6]  # ✅ Status de configuração do admin
                }
    except Exception as e:
        log.error(f"Erro ao buscar config da instância {instance_id}: {e}")
        return None


async def get_history(number: str, instance_id: str) -> List[Dict[str, str]]:
    """Busca histórico de conversas do banco"""
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 
                        CASE WHEN from_me THEN 'assistant' ELSE 'user' END as role,
                        content,
                        created_at
                    FROM messages
                    WHERE instance_id = %s AND chat_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (instance_id, number, MAX_HISTORY)
                )
                rows = cur.fetchall()
                # Inverte para ordem cronológica
                return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    except Exception as e:
        log.error(f"Erro ao buscar histórico: {e}")
        return []


async def save_message(instance_id: str, chatid: str, text: str, direction: str):
    """Salva mensagem no banco"""
    try:
        import time
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                from_me = (direction == "out")
                message_id = f"msg_{int(time.time() * 1000)}"
                timestamp = int(time.time())
                
                cur.execute(
                    """
                    INSERT INTO messages 
                    (instance_id, chat_id, content, from_me, message_id, timestamp, created_at, sender)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (instance_id, chatid, text, from_me, message_id, timestamp, datetime.utcnow(), chatid)
                )
                conn.commit()
    except Exception as e:
        log.warning(f"Erro ao salvar mensagem: {e}")


async def send_whatsapp_text(host: str, token: str, number: str, text: str) -> bool:
    """Envia mensagem de texto via UAZAPI"""
    try:
        url = f"https://{host}/send/text"
        headers = {"token": token, "Content-Type": "application/json"}
        payload = {
            "number": number,
            "text": text,
            "delay": int((MIN_TYPING_DELAY + MAX_TYPING_DELAY) / 2 * 1000)
        }
        
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return True
    except Exception as e:
        log.error(f"Erro ao enviar mensagem: {e}")
        return False


async def call_openai(history: List[Dict[str, str]], system_prompt: str) -> Optional[Dict[str, Any]]:
    """Chama OpenAI com function calling"""
    if not openai_client:
        log.error("OpenAI não configurado")
        return None
    
    tools = [
        {
            "type": "function",
            "function": {
                "name": "send_text",
                "description": "Envia mensagem de texto para o usuário",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Texto da mensagem"}
                    },
                    "required": ["message"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "handoff",
                "description": "Encaminha conversa para humano (Jonas)",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    ]
    
    try:
        messages = [{"role": "system", "content": system_prompt}] + history
        
        response = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=500
        )
        
        choice = response.choices[0].message
        return {
            "content": choice.content,
            "tool_calls": choice.tool_calls
        }
    except Exception as e:
        log.error(f"Erro OpenAI: {e}")
        return None


async def handoff_to_human(number: str, host: str, token: str, redirect_phone: str = ""):
    """Encaminha lead para humano - usa redirect_phone específico da instância"""
    # Prioriza redirect_phone da instância, depois fallback global
    target_phone = redirect_phone or REDIRECT_PHONE
    
    if not target_phone:
        log.error(f"❌ Handoff falhou: redirect_phone não configurado para lead {number}")
        return
    
    message = f"🔔 Novo lead para contato\n\nLead WhatsApp: {number}\n\nStatus: Demonstrou interesse e autorizou contato."
    
    await send_whatsapp_text(host, token, target_phone, message)
    log.info(f"✅ Lead {number} encaminhado para {target_phone}")


async def process_message(instance_id: str, number: str, text: str):
    """Processa mensagem e gera resposta"""
    if processing_lock.get(number):
        log.info(f"Já processando mensagem de {number}")
        return
    
    processing_lock[number] = True
    
    try:
        # Busca config da instância
        config = await get_instance_config(instance_id)
        if not config:
            log.warning(f"❌ Instância {instance_id} não encontrada no banco")
            return
        
        # Verificar se está conectada
        if config["status"] != "connected":
            log.warning(f"⚠️ Instância {instance_id} não conectada (status={config['status']})")
            return
        
        # Verificar se está configurada pelo admin
        admin_status = config.get("admin_status", "pending_config")
        if admin_status not in ["configured", "active"]:
            log.warning(f"⚠️ Instância {instance_id} ainda não configurada pelo admin (admin_status={admin_status})")
            return
        
        log.info(f"✅ Instância {instance_id} pronta para processar mensagens (status={config['status']}, admin_status={admin_status})")
        
        # Salva mensagem do usuário
        await save_message(instance_id, number, text, "in")
        
        # Busca histórico
        history = await get_history(number, instance_id)
        history.append({"role": "user", "content": text})
        
        # Chama IA
        response = await call_openai(history, config["prompt"])
        if not response:
            return
        
        # Processa tool calls
        tool_calls = response.get("tool_calls", [])
        if tool_calls:
            for call in tool_calls:
                if call.type != "function":
                    continue
                
                func_name = call.function.name
                func_args = json.loads(call.function.arguments)
                
                if func_name == "send_text":
                    msg = func_args.get("message", "")
                    if msg:
                        await send_whatsapp_text(config["host"], config["token"], number, msg)
                        await save_message(instance_id, number, msg, "out")
                        await asyncio.sleep(0.5)
                
                elif func_name == "handoff":
                    await handoff_to_human(number, config["host"], config["token"], config.get("redirect_phone", ""))
                    await save_message(instance_id, number, "[handoff]", "out")
        
        # Se não tem tool calls, envia conteúdo direto
        elif response.get("content"):
            msg = response["content"].strip()
            if msg:
                await send_whatsapp_text(config["host"], config["token"], number, msg)
                await save_message(instance_id, number, msg, "out")
    
    except Exception as e:
        log.error(f"Erro ao processar mensagem: {e}")
    finally:
        processing_lock[number] = False


# ==============================================================================
# ROTAS
# ==============================================================================
@router.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Webhook para receber mensagens do WhatsApp"""
    try:
        data = await request.json()
    except:
        data = {}
    
    # Extrai dados
    instance_id = data.get("instance_id") or data.get("instanceId") or data.get("instance")
    number = extract_number(data)
    text = extract_text(data)
    from_me = data.get("fromMe", False)
    
    if not instance_id or not number or from_me:
        return {"ok": True, "ignored": "missing_data_or_from_me"}
    
    if not text:
        return {"ok": True, "ignored": "no_text"}
    
    # Buffer de agregação (7 segundos)
    key = f"{instance_id}:{number}"
    now = datetime.now()
    
    if key in pending_messages:
        entry = pending_messages[key]
        entry["texts"].append(text)
        entry["last_update"] = now
        
        # Cancela timer anterior
        if "timer" in entry:
            entry["timer"].cancel()
        
        # Cria novo timer
        async def process_buffered():
            await asyncio.sleep(BUFFER_SECONDS)
            if key in pending_messages:
                entry = pending_messages.pop(key)
                combined_text = " ".join(entry["texts"])
                background_tasks.add_task(process_message, instance_id, number, combined_text)
        
        task = asyncio.create_task(process_buffered())
        entry["timer"] = task
    else:
        # Primeira mensagem - inicia buffer
        async def process_buffered():
            await asyncio.sleep(BUFFER_SECONDS)
            if key in pending_messages:
                entry = pending_messages.pop(key)
                combined_text = " ".join(entry["texts"])
                background_tasks.add_task(process_message, instance_id, number, combined_text)
        
        task = asyncio.create_task(process_buffered())
        pending_messages[key] = {
            "texts": [text],
            "last_update": now,
            "timer": task
        }
    
    return {"ok": True, "buffered": True}


@router.get("/webhook/health")
async def webhook_health():
    """Health check"""
    return {
        "ok": True,
        "openai_configured": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "redirect_configured": bool(REDIRECT_PHONE)
    }
