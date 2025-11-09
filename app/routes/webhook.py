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
        ["chat", "wa_lastMessageTextVote"],  # ← UAZAPI envia texto aqui!
        ["chat", "text"],
        ["chat", "lastMessage", "text"],
        ["chat", "lastMessage", "body"],
        ["data", "message", "conversation"],
        ["data", "text"],
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
        log.info(f"🔍 [CONFIG] Buscando instância: {instance_id}")
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
                    log.error(f"❌ [CONFIG] Instância {instance_id} NÃO EXISTE no banco!")
                    log.error(f"   Verifique se o ID está correto")
                    return None
                
                log.info(f"✅ [CONFIG] Instância encontrada no banco")
                log.info(f"   ID: {row['id']}")
                log.info(f"   Status: {row['status']}")
                log.info(f"   Admin Status: {row['admin_status']}")
                log.info(f"   Tem prompt: {'SIM' if row['prompt'] else 'NÃO'}")
                log.info(f"   Redirect phone: {row['redirect_phone'] or 'NÃO CONFIGURADO'}")
                
                # Se não tem prompt configurado, não processa (admin ainda não configurou)
                if not row['prompt']:
                    log.warning(f"⚠️ [CONFIG] Instância {instance_id} sem prompt configurado")
                    return None
                
                return {
                    "id": row['id'],
                    "host": row['uazapi_host'],
                    "token": row['uazapi_token'],
                    "prompt": row['prompt'],  # ✅ Prompt específico da instância (configurado pelo admin)
                    "status": row['status'],
                    "redirect_phone": row['redirect_phone'],  # ✅ Número específico da instância
                    "admin_status": row['admin_status']  # ✅ Status de configuração do admin
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
    """Chama OpenAI com function calling (igual TypeScript)"""
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
                "name": "send_menu",
                "description": "Envia menu interativo com botões de SIM/NÃO",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Texto da pergunta"},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Opções do menu (ex: ['sim', 'nao'])"
                        },
                        "footerText": {"type": "string", "description": "Texto do rodapé (opcional)"}
                    },
                    "required": ["text", "choices"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "handoff",
                "description": "Encaminha conversa para humano",
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
    """
    Processa mensagem com IA
    """
    try:
        log.info(f"🤖 [IA] INICIANDO - Mensagem de {number}: \"{text[:50]}...\"")
        
        # Lock para evitar processamento duplicado
        if processing_lock.get(number):
            log.warning(f"⚠️ [IA] Já processando. Ignorando duplicata.")
            return
        
        processing_lock[number] = True
        log.info(f"🔒 [IA] Lock adquirido")
        
    except Exception as e:
        log.error(f"❌ [IA] ERRO CRÍTICO NO INÍCIO: {e}")
        log.error(f"   Traceback: {str(e.__class__.__name__)}: {str(e)}")
        return
    
    try:
        # Buscar configuração da instância (prompt, token, redirect_phone)
        config = await get_instance_config(instance_id)
        
        if not config:
            log.error(f"❌ [IA] Configuração não encontrada!")
            return
        
        # ✅ VERIFICAÇÃO: admin_status deve ser 'configured' ou 'active'
        admin_status = config.get("admin_status", "")
        if admin_status not in ["configured", "active"]:
            log.warning(f"⚠️ [IA] Instância não configurada pelo admin (status: {admin_status})")
            return
        
        # ✅ VERIFICAÇÃO CRÍTICA: Ignorar se desconectado
        if config["status"] != "connected":
            log.warning(f"⚠️ [IA] WhatsApp desconectado (status: {config['status']})")
            return
        
        # Salva mensagem do usuário
        await save_message(instance_id, number, text, "in")
        
        # Busca histórico
        history = await get_history(number, instance_id)
        history.append({"role": "user", "content": text})
        log.info(f"📜 [IA] Histórico: {len(history)} mensagens")
        
        # Chama IA
        log.info(f"🧠 [IA] Chamando OpenAI ({OPENAI_MODEL})...")
        response = await call_openai(history, config["prompt"])
        
        if not response:
            log.error(f"❌ [IA] OpenAI falhou!")
            return
        
        log.info(f"✅ [IA] OpenAI respondeu")
        
        # Processa tool calls (igual TypeScript - processa TODAS em sequência)
        tool_calls = response.get("tool_calls", [])
        if tool_calls:
            log.info(f"🤖 [IA] {len(tool_calls)} função(ões) detectada(s)")
            
            for call in tool_calls:
                if call.type != "function":
                    continue
                
                func_name = call.function.name
                func_args = json.loads(call.function.arguments)
                
                log.info(f"   🔧 Executando: {func_name}")
                
                if func_name == "send_text":
                    msg = func_args.get("message", "")
                    if msg:
                        log.info(f"📤 [IA] Enviando: \"{msg[:100]}{'...' if len(msg) > 100 else ''}\"")
                        await send_whatsapp_text(config["host"], config["token"], number, msg)
                        await save_message(instance_id, number, msg, "out")
                        log.info(f"✅ [IA] Mensagem enviada com sucesso")
                        await asyncio.sleep(0.5)
                
                elif func_name == "send_menu":
                    # Menu com botões (igual TypeScript)
                    text = func_args.get("text", "")
                    choices = func_args.get("choices", ["sim", "nao"])
                    footer = func_args.get("footerText", "Escolha uma opção")
                    
                    if text:
                        # Por enquanto, envia como texto simples
                        # TODO: Implementar botões nativos da UAZAPI
                        menu_text = f"{text}\n\n"
                        for i, choice in enumerate(choices, 1):
                            menu_text += f"{i}. {choice.upper()}\n"
                        menu_text += f"\n{footer}"
                        
                        await send_whatsapp_text(config["host"], config["token"], number, menu_text)
                        await save_message(instance_id, number, text, "out")
                        log.info(f"   ✅ send_menu executado: {len(choices)} opções")
                        await asyncio.sleep(0.5)
                
                elif func_name == "handoff":
                    log.info(f"   🎯 HANDOFF detectado!")
                    await handoff_to_human(number, config["host"], config["token"], config.get("redirect_phone", ""))
                    await save_message(instance_id, number, "[handoff]", "out")
                    log.info(f"   ✅ handoff executado")
                
                else:
                    log.warning(f"   ❌ Função desconhecida: {func_name}")
        
        # Se não tem tool calls, envia conteúdo direto
        elif response.get("content"):
            msg = response["content"].strip()
            if msg:
                log.info(f"📤 [IA] Enviando resposta direta: \"{msg[:100]}{'...' if len(msg) > 100 else ''}\"")
                await send_whatsapp_text(config["host"], config["token"], number, msg)
                await save_message(instance_id, number, msg, "out")
                log.info(f"✅ [IA] Mensagem enviada com sucesso")
    
    except Exception as e:
        log.error(f"❌ [IA] ERRO FATAL ao processar mensagem!")
        log.error(f"   Tipo: {e.__class__.__name__}")
        log.error(f"   Mensagem: {str(e)}")
        import traceback
        log.error(f"   Traceback completo:\n{traceback.format_exc()}")
    finally:
        processing_lock[number] = False
        log.info(f"🔓 [IA] Lock liberado para {number}")


# ==============================================================================
# ROTAS
# ==============================================================================
@router.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Webhook para receber mensagens do WhatsApp"""
    try:
        data = await request.json()
    except Exception as e:
        log.error(f"❌ [WEBHOOK] Erro ao parsear JSON: {e}")
        data = {}
    
    # Extrai dados
    # UAZAPI envia "owner" que é o telefone da instância
    chat = data.get("chat", {})
    owner = chat.get("owner")  # Telefone da instância (ex: 553188379840)
    
    log.info(f"🔍 [WEBHOOK] Owner extraído do payload: {owner}")
    
    # Buscar instância pelo owner (phone_number)
    instance_id = None
    if owner:
        try:
            pool = get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    # Primeiro, ver quantas instâncias existem com esse número
                    cur.execute(
                        "SELECT COUNT(*) FROM instances WHERE phone_number = %s",
                        (owner,)
                    )
                    result = cur.fetchone()
                    count = result['count'] if result else 0
                    log.info(f"🔍 [WEBHOOK] Instâncias encontradas com phone_number={owner}: {count}")
                    
                    # Buscar a conectada e ativa
                    cur.execute(
                        "SELECT id, status, admin_status FROM instances WHERE phone_number = %s ORDER BY created_at DESC LIMIT 5",
                        (owner,)
                    )
                    rows = cur.fetchall()
                    
                    if rows:
                        log.info(f"🔍 [WEBHOOK] Instâncias encontradas:")
                        for row in rows:
                            log.info(f"   - ID: {row['id']}, Status: {row['status']}, Admin: {row['admin_status']}")
                        
                        # Pegar a primeira que está connected
                        for row in rows:
                            if row['status'] == 'connected':
                                instance_id = row['id']
                                log.info(f"✅ [WEBHOOK] Usando instância: {instance_id}")
                                break
                        
                        if not instance_id and rows:
                            # Se nenhuma connected, usa a mais recente
                            instance_id = rows[0]['id']
                            log.warning(f"⚠️ [WEBHOOK] Nenhuma connected, usando mais recente: {instance_id}")
                    else:
                        log.error(f"❌ [WEBHOOK] Nenhuma instância com phone_number={owner}")
                    
        except Exception as e:
            log.error(f"❌ [WEBHOOK] Erro ao buscar instância por owner: {e}")
            import traceback
            log.error(traceback.format_exc())
    else:
        log.error(f"❌ [WEBHOOK] Owner não encontrado no payload!")
    
    number = extract_number(data)
    text = extract_text(data)
    from_me = data.get("fromMe", False)
    
    # Log simplificado
    log.info(f"📥 [WEBHOOK] {number}: \"{text[:50]}{'...' if len(text) > 50 else ''}\" (instance: {instance_id})")
    
    if not instance_id:
        log.warning("⚠️ [WEBHOOK] Instance ID não encontrado! Ignorando.")
        return {"ok": True, "ignored": "no_instance_id"}
    
    if not number:
        log.warning("⚠️ [WEBHOOK] Número não encontrado! Ignorando.")
        return {"ok": True, "ignored": "no_number"}
    
    if from_me:
        log.info("ℹ️ [WEBHOOK] Mensagem enviada por mim (from_me=True). Ignorando.")
        return {"ok": True, "ignored": "from_me"}
    
    if not text:
        log.warning("⚠️ [WEBHOOK] Texto vazio! Ignorando.")
        return {"ok": True, "ignored": "no_text"}
    
    # Buffer de agregação (7 segundos)
    key = f"{instance_id}:{number}"
    now = datetime.now()
    
    if key in pending_messages:
        entry = pending_messages[key]
        entry["texts"].append(text)
        entry["last_update"] = now
        
        log.info(f"⏱️ [BUFFER] +1 mensagem ({len(entry['texts'])} total). Resetando timer...")
        
        # Cancela timer anterior
        if "timer" in entry:
            entry["timer"].cancel()
        
        # Cria novo timer
        async def process_buffered():
            await asyncio.sleep(BUFFER_SECONDS)
            if key in pending_messages:
                entry = pending_messages.pop(key)
                combined_text = " ".join(entry["texts"])
                log.info(f"🚀 [BUFFER] Processando {len(entry['texts'])} mensagem(s): \"{combined_text[:100]}...\"")
                log.info(f"🔄 [BUFFER] Criando task para processar mensagem...")
                # Usar asyncio.create_task ao invés de background_tasks
                # porque background_tasks só executa APÓS resposta HTTP
                asyncio.create_task(process_message(instance_id, number, combined_text))
                log.info(f"✅ [BUFFER] Task criada e iniciada")
        
        task = asyncio.create_task(process_buffered())
        entry["timer"] = task
    else:
        log.info(f"⏱️ [BUFFER] Aguardando {BUFFER_SECONDS}s...")
        
        # Primeira mensagem - inicia buffer
        async def process_buffered():
            await asyncio.sleep(BUFFER_SECONDS)
            if key in pending_messages:
                entry = pending_messages.pop(key)
                combined_text = " ".join(entry["texts"])
                log.info(f"🚀 [BUFFER] Processando: \"{combined_text[:100]}...\"")
                log.info(f"🔄 [BUFFER] Criando task para processar mensagem...")
                # Usar asyncio.create_task ao invés de background_tasks
                asyncio.create_task(process_message(instance_id, number, combined_text))
                log.info(f"✅ [BUFFER] Task criada e iniciada")
        
        task = asyncio.create_task(process_buffered())
        pending_messages[key] = {
            "texts": [text],
            "last_update": now,
            "timer": task
        }
    
    return {"ok": True, "buffered": True}


@router.post("/webhook/status")
async def whatsapp_status_webhook(request: Request):
    """
    Webhook para receber eventos de status do WhatsApp (conexão/desconexão)
    A UAZAPI envia eventos quando o WhatsApp conecta ou desconecta
    """
    try:
        data = await request.json()
    except:
        data = {}
    
    log.info(f"[WEBHOOK STATUS] Evento recebido: {data}")
    
    # Extrair dados
    instance_id = data.get("instance_id") or data.get("instanceId") or data.get("instance")
    event = data.get("event") or data.get("type")
    status = data.get("status")
    state = data.get("state")
    
    if not instance_id:
        return {"ok": True, "ignored": "no_instance_id"}
    
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Buscar instância
                cur.execute("SELECT id, status FROM instances WHERE id = %s", (instance_id,))
                instance = cur.fetchone()
                
                if not instance:
                    log.warning(f"[WEBHOOK STATUS] Instância {instance_id} não encontrada")
                    return {"ok": True, "ignored": "instance_not_found"}
                
                current_status = instance[1]
                new_status = current_status
                
                # Determinar novo status baseado no evento
                # A UAZAPI pode enviar diferentes tipos de eventos
                if event in ["disconnect", "disconnected", "close", "closed"]:
                    new_status = "disconnected"
                    log.warning(f"⚠️ [DESCONEXÃO] Instância {instance_id} DESCONECTADA!")
                    
                elif event in ["connect", "connected", "open", "ready"]:
                    new_status = "connected"
                    log.info(f"✅ [CONEXÃO] Instância {instance_id} conectada")
                    
                elif status == "close" or state == "close":
                    new_status = "disconnected"
                    log.warning(f"⚠️ [DESCONEXÃO] Instância {instance_id} DESCONECTADA (status close)!")
                    
                elif status == "open" or state == "open":
                    new_status = "connected"
                    log.info(f"✅ [CONEXÃO] Instância {instance_id} conectada (status open)")
                
                # Atualizar status no banco se mudou
                if new_status != current_status:
                    cur.execute("""
                        UPDATE instances
                        SET status = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (new_status, instance_id))
                    
                    conn.commit()
                    
                    log.info(f"✅ Status atualizado: {instance_id} → {new_status}")
                    
                    # Se desconectou, registrar no log
                    if new_status == "disconnected":
                        cur.execute("""
                            INSERT INTO admin_actions 
                            (admin_id, action_type, target_type, target_id, description, created_at)
                            VALUES (1, 'instance_disconnected', 'instance', %s, 
                                    'WhatsApp desconectado automaticamente', NOW())
                        """, (instance_id,))
                        conn.commit()
                
                return {"ok": True, "status_updated": new_status != current_status, "new_status": new_status}
                
    except Exception as e:
        log.error(f"[WEBHOOK STATUS] Erro ao processar evento: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


@router.get("/webhook/health")
async def webhook_health():
    """Health check"""
    return {
        "ok": True,
        "openai_configured": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
        "redirect_configured": bool(REDIRECT_PHONE)
    }
