# app/core/stage.py
import unicodedata
from typing import Any, Dict, List, Tuple

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s

# ---- Regras (mesmas do front) ----
HOT_HINTS = list(map(_norm, [
    "vou te passar para","vou te passar pro","vou passar voce para","vou passar você para",
    "vou passar para o setor","vou passar para o departamento","vou passar para o time",
    "vou passar seu contato","vou passar o seu contato","vou passar seu numero","vou passar o seu numero",
    "vou repassar seu contato","repassei seu contato","enviei seu contato","vou enviar seu contato","enviarei seu contato",
    "vou encaminhar","encaminhando seu contato","encaminhei seu contato","encaminhei seu numero","encaminhei seu número",
    "encaminhar seu contato","estou encaminhando","encaminharei",
    "vou te colocar em contato","vou colocar voce em contato","vou colocar você em contato","colocar voce em contato","colocar você em contato",
    "vou te conectar","vou te por em contato","te coloco em contato",
    "o time comercial vai te chamar","o time vai te chamar","nossa equipe vai entrar em contato","a equipe vai entrar em contato","o setor vai entrar em contato",
    "o atendente vai falar com voce","o atendente vai falar com você","um atendente vai te chamar","um consultor vai te chamar","o consultor vai te chamar",
    "o especialista vai te chamar","o responsavel vai te chamar","o responsável vai te chamar","o pessoal do comercial te chama","suporte vai te chamar",
    "vendas vai te chamar","pre-vendas vai te chamar","pré-vendas vai te chamar",
    "vou pedir para alguem te chamar","vou pedir para alguém te chamar","vou pedir pra alguem te chamar","vou pedir pra alguém te chamar",
    "vou pedir pro pessoal te chamar","vou pedir para o time te chamar","ja pedi para te chamarem","já pedi para te chamarem",
    "vou transferir","estou transferindo","transferencia para o setor","transferência para o setor",
    "transferi sua solicitacao","transferi sua solicitação","direcionei seu contato","direcionando seu contato","direcionar seu contato",
    "daqui a pouco te chamam","em breve vao entrar em contato","em breve vão entrar em contato","abrirei um chamado","vou abrir um chamado","abrir um ticket","abrirei um ticket",
]))
HOT_NEGATIVE_GUARDS = list(map(_norm, [
    "cardapio","cardápio","menu","catalogo","catálogo","ver menu","ver cardapio",
    "acesse o menu","acesse o cardapio","acesse o cardápio","acesse nosso catalogo","acesse nosso catálogo",
    "cardapio online","link do menu","nosso menu","veja o menu","veja o cardapio","veja o catálogo",
]))
LEAD_OK_PATTERNS = list(map(_norm, [
    "sim, pode continuar","sim pode continuar","pode continuar","ok, pode continuar","ok pode continuar",
    "pode seguir","sim, pode seguir","sim pode seguir","vamos continuar","podemos continuar",
    "pode prosseguir","ok vamos prosseguir","segue","segue por favor","pode mostrar","pode me mostrar",
    "pode enviar","pode mandar","pode continuar 👍","pode continuar sim","sim, pode continuar sim",
    "pode continuar por favor","pode continuar pf","pode continuar pff","pode cont","pode cnt","pode seg",
    "pode prosseg","pode proseguir",
]))
LEAD_NAME_PATTERNS = list(map(_norm, [
    "qual seu nome","qual o seu nome","me diga seu nome","me fala seu nome","como voce se chama","como você se chama",
    "quem fala","quem esta falando","quem está falando","quem e voce","quem é você","pode me dizer seu nome",
    "me passa seu nome","me informe seu nome","seu nome por favor","nome pfv","nome por favor","nome?","qual seu primeiro nome",
    "qual seu nome completo","nome do cliente","nome do titular","nome para cadastro","poderia me informar seu nome","me diga o seu nome",
    "informe seu nome","sobrenome","seu nome e sobrenome","como devo te chamar","como posso te chamar","qual e seu nome","qual é seu nome","qual seria seu nome",
    "ql seu nome","q seu nome","seu nm","seu nome sff","seu nome pf",
]))

def make_transcript_key(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "empty"
    last_ts = items[0].get("messageTimestamp") or items[0].get("timestamp") or 0
    tail = (items[0].get("text") or items[0].get("body") or "")[:64]
    return f"{len(items)}:{last_ts}:{len(tail)}"

def _is_from_me(m: Dict[str, Any]) -> bool:
    return bool(m.get("fromMe") or m.get("fromme") or m.get("from_me"))

def _extract_text(m: Dict[str, Any]) -> str:
    mm = m.get("message") or {}
    return _norm(
        m.get("text")
        or m.get("caption")
        or m.get("body")
        or mm.get("text")
        or mm.get("conversation")
        or (mm.get("extendedTextMessage") or {}).get("text")
        or ""
    )

def classify(items: List[Dict[str, Any]]) -> str:
    stage = "contatos"
    for m in items:
        if not _is_from_me(m):
            continue
        text = _extract_text(m)
        if not text:
            continue
        has_menu = any(g in text for g in HOT_NEGATIVE_GUARDS)
        if (not has_menu) and any(h in text for h in HOT_HINTS):
            return "lead_quente"
        if any(p in text for p in LEAD_OK_PATTERNS):
            stage = "lead" if stage != "lead_quente" else stage
        if any(p in text for p in LEAD_NAME_PATTERNS):
            stage = "lead" if stage != "lead_quente" else stage
    return stage

# cache simples em memória (evita reprocessar a cada /messages)
_STAGE_CACHE: Dict[str, Dict[str, Any]] = {}  # chatid -> {stage,lastKey,at}

def classify_and_cache(chatid: str, items: List[Dict[str, Any]]) -> Tuple[str, str]:
    key = make_transcript_key(items)
    rec = __STAGE_CACHE.get(chatid)
    if rec and rec.get("lastKey") == key:
        return rec["stage"], key
    stage = classify(items)
    __STAGE_CACHE[chatid] = {"stage": stage, "lastKey": key}
    return stage, key

def get_cached_stage(chatid: str) -> str | None:
    rec = _STATE_CACHE.get(chatid) if (_STATE_CACHE := _STAGE_CACHE) else None  # py<=3.9 compat
    return rec.get("stage") if rec else None
