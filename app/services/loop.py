import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timezone, date, time as dtime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.pg import get_pool
from app.services import uazapi

log = logging.getLogger("uvicorn.error")

DEFAULT_DAILY_LIMIT = 30
MIN_DELAY_SECONDS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_settings(instance_id: str) -> Dict[str, Any]:
    """Garantir registro na tabela de configurações e retorná-lo."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instance_loop_settings (instance_id)
                VALUES (%s)
                ON CONFLICT (instance_id) DO NOTHING
                """,
                (instance_id,),
            )
            cur.execute(
                """
                SELECT instance_id, auto_run, ia_auto, COALESCE(daily_limit, %s) AS daily_limit,
                       message_template, window_start, window_end,
                       last_run_at, loop_status, updated_at
                  FROM instance_loop_settings
                 WHERE instance_id = %s
                """,
                (DEFAULT_DAILY_LIMIT, instance_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Instância não encontrada nas configurações")
    return dict(row)


def get_loop_settings(instance_id: str) -> Dict[str, Any]:
    settings = _ensure_settings(instance_id)
    return {
        "instance_id": settings["instance_id"],
        "auto_run": bool(settings["auto_run"]),
        "ia_auto": bool(settings["ia_auto"]),
        "daily_limit": int(settings["daily_limit"] or DEFAULT_DAILY_LIMIT),
        "message_template": settings.get("message_template") or "",
        "window_start": settings.get("window_start").isoformat() if settings.get("window_start") else None,
        "window_end": settings.get("window_end").isoformat() if settings.get("window_end") else None,
        "last_run_at": settings.get("last_run_at").isoformat() if settings.get("last_run_at") else None,
        "loop_status": settings.get("loop_status"),
        "updated_at": settings.get("updated_at").isoformat() if settings.get("updated_at") else None,
    }


def update_loop_settings(instance_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    settings = _ensure_settings(instance_id)
    auto_run = bool(data.get("auto_run", settings["auto_run"]))
    ia_auto = bool(data.get("ia_auto", settings["ia_auto"]))
    daily_limit = data.get("daily_limit", settings["daily_limit"] or DEFAULT_DAILY_LIMIT)
    message_template = data.get("message_template", settings.get("message_template"))
    window_start = data.get("window_start") or settings.get("window_start")
    window_end = data.get("window_end") or settings.get("window_end")

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE instance_loop_settings
                   SET auto_run = %s,
                       ia_auto = %s,
                       daily_limit = %s,
                       message_template = %s,
                       window_start = %s,
                       window_end = %s,
                       updated_at = NOW()
                 WHERE instance_id = %s
                """,
                (
                    auto_run,
                    ia_auto,
                    int(daily_limit) if daily_limit else None,
                    message_template,
                    window_start,
                    window_end,
                    instance_id,
                ),
            )
    return get_loop_settings(instance_id)


def _normalize_phone(phone: str) -> str:
    digits = "".join(filter(str.isdigit, phone or ""))
    return digits


def add_contact(instance_id: str, name: Optional[str], phone: str, niche: Optional[str], source: str = "manual") -> Dict[str, Any]:
    if not phone:
        raise HTTPException(status_code=400, detail="Telefone é obrigatório")
    norm_phone = _normalize_phone(phone)
    if not norm_phone:
        raise HTTPException(status_code=400, detail="Telefone inválido")

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Verifica se já foi enviado anteriormente
            cur.execute(
                """
                SELECT mensagem_enviada
                  FROM instance_loop_totals
                 WHERE instance_id = %s AND phone = %s
                """,
                (instance_id, norm_phone),
            )
            row = cur.fetchone()
            if row and row["mensagem_enviada"]:
                return {"status": "skipped", "reason": "already_sent"}

            cur.execute(
                """
                INSERT INTO instance_loop_queue (instance_id, name, phone, niche, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id, created_at
                """,
                (instance_id, name, norm_phone, niche, source),
            )
            inserted = cur.fetchone()
    if not inserted:
        return {"status": "skipped", "reason": "duplicate_in_queue"}
    return {"status": "ok", "id": inserted["id"], "created_at": inserted["created_at"].isoformat() if inserted["created_at"] else None}


def import_contacts_from_csv(instance_id: str, file_bytes: bytes) -> Dict[str, int]:
    text = file_bytes.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"inserted": 0, "skipped": 0, "errors": 0}

    header = rows[0]
    name_idx = phone_idx = niche_idx = None
    normalized = [h.strip().lower() for h in header]
    for idx, h in enumerate(normalized):
        if h in {"nome", "name", "full_name", "contato"} and name_idx is None:
            name_idx = idx
        if h in {"telefone", "phone", "numero", "número", "whatsapp"} and phone_idx is None:
            phone_idx = idx
        if h in {"nicho", "niche", "segmento", "categoria"} and niche_idx is None:
            niche_idx = idx

    if phone_idx is None:
        raise HTTPException(status_code=400, detail="CSV precisa de coluna de telefone")

    inserted = skipped = errors = 0
    for row in rows[1:]:
        try:
            name = row[name_idx].strip() if name_idx is not None and name_idx < len(row) else ""
            phone = row[phone_idx].strip() if phone_idx is not None and phone_idx < len(row) else ""
            niche = row[niche_idx].strip() if niche_idx is not None and niche_idx < len(row) else None
            result = add_contact(instance_id, name or None, phone, niche, source="csv")
            if result.get("status") == "ok":
                inserted += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Erro ao importar linha CSV: %s", exc)
            errors += 1
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def list_queue(instance_id: str, page: int = 1, page_size: int = 50, search: Optional[str] = None) -> Dict[str, Any]:
    offset = max(page - 1, 0) * page_size
    params: List[Any] = [instance_id]
    where = "instance_id = %s"
    if search:
        params.append(f"%{search.lower()}%")
        where += " AND (LOWER(name) LIKE %s OR phone LIKE %s)"
        params.append(params[-1])
    query = f"""
        SELECT id, name, phone, niche, source, status, created_at
          FROM instance_loop_queue
         WHERE {where}
         ORDER BY created_at ASC
         LIMIT %s OFFSET %s
    """
    params.extend([page_size, offset])

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall() or []
            cur.execute(
                f"SELECT COUNT(*) AS count FROM instance_loop_queue WHERE {where}",
                tuple(params[:-2]),
            )
            total = cur.fetchone()["count"] if cur.rowcount else 0
    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "phone": r["phone"],
                "niche": r.get("niche"),
                "source": r.get("source"),
                "status": r.get("status"),
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_totals(instance_id: str, page: int = 1, page_size: int = 50, search: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
    offset = max(page - 1, 0) * page_size
    clauses = ["instance_id = %s"]
    params: List[Any] = [instance_id]
    if search:
        clauses.append("(LOWER(name) LIKE %s OR phone LIKE %s)")
        params.append(f"%{search.lower()}%")
        params.append(params[-1])
    if status == "sent":
        clauses.append("mensagem_enviada = TRUE")
    elif status == "pending":
        clauses.append("mensagem_enviada = FALSE")
    where = " AND ".join(clauses)

    query = f"""
        SELECT id, name, phone, niche, mensagem_enviada, status, updated_at
          FROM instance_loop_totals
         WHERE {where}
         ORDER BY updated_at DESC
         LIMIT %s OFFSET %s
    """
    params.extend([page_size, offset])

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall() or []
            cur.execute(
                f"SELECT COUNT(*) AS count FROM instance_loop_totals WHERE {where}",
                tuple(params[:-2]),
            )
            total = cur.fetchone()["count"] if cur.rowcount else 0
    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "phone": r["phone"],
                "niche": r.get("niche"),
                "sent": bool(r.get("mensagem_enviada")),
                "status": r.get("status"),
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _update_loop_status(instance_id: str, status: str, *, last_run: bool = False) -> None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE instance_loop_settings
                   SET loop_status = %s,
                       last_run_at = CASE WHEN %s THEN NOW() ELSE last_run_at END,
                       updated_at = NOW()
                 WHERE instance_id = %s
                """,
                (status, last_run, instance_id),
            )


def _record_event(instance_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO instance_loop_events (instance_id, event_type, payload)
                VALUES (%s, %s, %s::jsonb)
                """,
                (instance_id, event_type, json.dumps(payload)),
            )


def get_recent_events(instance_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, payload, created_at
                  FROM instance_loop_events
                 WHERE instance_id = %s
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (instance_id, limit),
            )
            rows = cur.fetchall() or []
    events = []
    for r in reversed(rows):  # Em ordem cronológica
        payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        payload.update({"type": r["event_type"], "at": r["created_at"].isoformat() if r.get("created_at") else None})
        events.append(payload)
    return events


def _compute_daily_sent(instance_id: str) -> Tuple[int, int]:
    settings = _ensure_settings(instance_id)
    daily_cap = int(settings.get("daily_limit") or DEFAULT_DAILY_LIMIT)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS count
                  FROM instance_loop_totals
                 WHERE instance_id = %s
                   AND mensagem_enviada = TRUE
                   AND updated_at::date = CURRENT_DATE
                """,
                (instance_id,),
            )
            row = cur.fetchone() or {"count": 0}
    return daily_cap, int(row["count"] or 0)


def get_loop_state(instance_id: str, *, include_queue_size: bool = True) -> Dict[str, Any]:
    settings = get_loop_settings(instance_id)
    daily_cap, sent_today = _compute_daily_sent(instance_id)
    queue_pending = 0
    if include_queue_size:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM instance_loop_queue WHERE instance_id = %s AND status = 'pending'",
                    (instance_id,),
                )
                queue_pending = cur.fetchone()["count"] if cur.rowcount else 0
    remaining_today = max(0, daily_cap - sent_today)
    actually_running = loop_manager.is_running(instance_id)
    return {
        "cap": daily_cap,
        "sent_today": sent_today,
        "remaining_today": remaining_today,
        "loop_status": settings["loop_status"],
        "last_run_at": settings.get("last_run_at"),
        "auto_run": settings["auto_run"],
        "ia_auto": settings["ia_auto"],
        "message_template": settings["message_template"],
        "queue_pending": queue_pending,
        "actually_running": actually_running,
    }


class LoopManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stop_flags: Dict[str, asyncio.Event] = {}
        self._listeners: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    def is_running(self, instance_id: str) -> bool:
        task = self._tasks.get(instance_id)
        return task is not None and not task.done()

    async def start_loop(self, instance_id: str, *, admin_id: Optional[int] = None) -> Dict[str, Any]:
        async with self._lock:
            if self.is_running(instance_id):
                raise HTTPException(status_code=409, detail="Loop já está em execução")
            stop_event = asyncio.Event()
            self._stop_flags[instance_id] = stop_event
            task = asyncio.create_task(self._run_loop(instance_id, stop_event, admin_id=admin_id))
            self._tasks[instance_id] = task
        return {"status": "started"}

    async def stop_loop(self, instance_id: str) -> Dict[str, Any]:
        async with self._lock:
            stop_event = self._stop_flags.get(instance_id)
            if not stop_event:
                raise HTTPException(status_code=404, detail="Loop não está em execução")
            stop_event.set()
        return {"status": "stopping"}

    async def _run_loop(self, instance_id: str, stop_event: asyncio.Event, *, admin_id: Optional[int] = None) -> None:
        log.info("[LOOP] Iniciando processamento para instância %s", instance_id)
        settings = _ensure_settings(instance_id)
        _update_loop_status(instance_id, "running")
        self._publish(instance_id, "start", {"admin_id": admin_id})

        try:
            daily_cap, sent_today = _compute_daily_sent(instance_id)
            if sent_today >= daily_cap:
                log.info("[LOOP] Instância %s já atingiu cota diária", instance_id)
                self._publish(instance_id, "end", {"reason": "daily_quota", "processed": 0})
                return

            host_token = self._get_instance_credentials(instance_id)
            if not host_token:
                raise RuntimeError("Instância sem token/host configurado")
            host, token = host_token

            processed = 0
            while not stop_event.is_set():
                if not self._within_window(settings):
                    self._publish(instance_id, "pause", {"reason": "outside_window"})
                    await asyncio.sleep(60)
                    continue

                contact = self._next_contact(instance_id)
                if not contact:
                    log.info("[LOOP] Nenhum contato na fila para %s", instance_id)
                    break

                if sent_today >= daily_cap:
                    self._publish(instance_id, "end", {"reason": "daily_quota", "processed": processed})
                    break

                contact_id, name, phone, niche = contact
                event_payload = {"name": name, "phone": phone, "niche": niche}

                try:
                    template = settings.get("message_template") or "Olá {name}, tudo bem?"
                    message = template.replace("{name}", name or "").replace("{{name}}", name or "")
                    await uazapi.send_text(instance_id, token, phone, message)
                    self._mark_contact_sent(instance_id, contact_id, name, phone, niche, True, "sent")
                    processed += 1
                    sent_today += 1
                    event_payload.update({"status": "sent"})
                    self._publish(instance_id, "item", event_payload)
                except Exception as exc:  # noqa: BLE001
                    log.error("[LOOP] Falha ao enviar para %s: %s", phone, exc)
                    self._mark_contact_sent(instance_id, contact_id, name, phone, niche, False, "error")
                    event_payload.update({"status": "error", "error": str(exc)})
                    self._publish(instance_id, "item", event_payload)

                await asyncio.sleep(MIN_DELAY_SECONDS)

            if stop_event.is_set():
                self._publish(instance_id, "end", {"reason": "manual_stop", "processed": processed})
            else:
                self._publish(instance_id, "end", {"reason": "completed", "processed": processed})
        except Exception as exc:  # noqa: BLE001
            log.exception("[LOOP] Erro crítico na instância %s", instance_id)
            self._publish(instance_id, "error", {"message": str(exc)})
        finally:
            _update_loop_status(instance_id, "idle", last_run=True)
            async with self._lock:
                self._tasks.pop(instance_id, None)
                self._stop_flags.pop(instance_id, None)
            log.info("[LOOP] Finalizado processamento para %s", instance_id)

    def _within_window(self, settings: Dict[str, Any]) -> bool:
        start: Optional[dtime] = settings.get("window_start")
        end: Optional[dtime] = settings.get("window_end")
        if not start or not end:
            return True
        now_time = datetime.now().time()
        if start <= end:
            return start <= now_time <= end
        # janela passando de meia-noite
        return now_time >= start or now_time <= end

    def _next_contact(self, instance_id: str) -> Optional[Tuple[int, Optional[str], str, Optional[str]]]:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, phone, niche
                      FROM instance_loop_queue
                     WHERE instance_id = %s AND status = 'pending'
                     ORDER BY created_at ASC
                     LIMIT 1
                    """,
                    (instance_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return row["id"], row.get("name"), row.get("phone"), row.get("niche")

    def _mark_contact_sent(
        self,
        instance_id: str,
        queue_id: int,
        name: Optional[str],
        phone: str,
        niche: Optional[str],
        sent: bool,
        status: str,
    ) -> None:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM instance_loop_queue WHERE id = %s", (queue_id,))
                cur.execute(
                    """
                    INSERT INTO instance_loop_totals (instance_id, name, phone, niche, mensagem_enviada, status, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (instance_id, phone)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        niche = EXCLUDED.niche,
                        mensagem_enviada = EXCLUDED.mensagem_enviada,
                        status = EXCLUDED.status,
                        updated_at = NOW()
                    """,
                    (instance_id, name, phone, niche, sent, status),
                )

    def _get_instance_credentials(self, instance_id: str) -> Optional[Tuple[str, str]]:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT host, token FROM instances WHERE id = %s",
                    (instance_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        host = row.get("host") or row.get("uazapi_host")
        token = row.get("token") or row.get("uazapi_token")
        if not host or not token:
            return None
        return host, token

    def _publish(self, instance_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload.setdefault("type", event_type)
        payload.setdefault("at", _now().isoformat())
        try:
            _record_event(instance_id, event_type, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao registrar evento do loop: %s", exc)
        queues = self._listeners.get(instance_id) or []
        for q in list(queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def subscribe(self, instance_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._listeners.setdefault(instance_id, []).append(queue)
        return queue

    def unsubscribe(self, instance_id: str, queue: asyncio.Queue) -> None:
        listeners = self._listeners.get(instance_id)
        if not listeners:
            return
        if queue in listeners:
            listeners.remove(queue)
        if not listeners:
            self._listeners.pop(instance_id, None)


loop_manager = LoopManager()


async def stream_progress(instance_id: str) -> AsyncIterator[str]:
    history = get_recent_events(instance_id)
    queue = loop_manager.subscribe(instance_id)
    try:
        for event in history:
            yield f"data: {json.dumps(event)}\n\n"
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
    finally:
        loop_manager.unsubscribe(instance_id, queue)


async def request_loop_start(instance_id: str, *, admin_id: Optional[int] = None) -> Dict[str, Any]:
    return await loop_manager.start_loop(instance_id, admin_id=admin_id)


async def request_loop_stop(instance_id: str) -> Dict[str, Any]:
    return await loop_manager.stop_loop(instance_id)
