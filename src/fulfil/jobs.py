"""Фоновый опрос WB: заказы и их статусы по каждому клиенту (Этап 3 плана №3, п.3.1).

Статусы уже загруженных заказов раньше не обновлялись — опрашивался только
GET /orders/new, поэтому отмена покупателем и «в доставке»/«принята» до нас
не доходили. Здесь этот опрос выполняется по расписанию, а не только по клику
«Обновить из WB».

uvicorn в этом проекте работает в одном процессе (см. Dockerfile) — фоновый
поток и process-local Event достаточны. При масштабировании на несколько
воркеров/процессов нужен pg_try_advisory_lock, иначе несколько процессов
будут гонять синк параллельно и упираться в лимиты WB быстрее, чем один.
"""

import logging
import threading

from sqlalchemy import select

from fulfil.config import get_settings
from fulfil.db import SessionLocal
from fulfil.errors import AppError
from fulfil.integrations.wb import get_wb_client
from fulfil.models.client import Client
from fulfil.services.orders import refresh_order_statuses, sync_orders_from_wb

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def run_sync_cycle() -> None:
    """Один проход по всем активным клиентам с ключом и складом — публичная
    функция, чтобы её можно было дёрнуть и вручную (не только из фонового потока)."""
    db = SessionLocal()
    try:
        clients = list(
            db.scalars(
                select(Client).where(
                    Client.archived_at.is_(None),
                    Client.wb_api_key_enc.is_not(None),
                    Client.wb_warehouse_id.is_not(None),
                )
            )
        )
        for client in clients:
            try:
                wb_client = get_wb_client(client)
                sync_orders_from_wb(db, client, wb_client)
                refresh_order_statuses(db, client, wb_client)
            except AppError:
                # sync_orders_from_wb/refresh_order_statuses уже записали текст
                # причины в client.last_sync_error и закоммитили — здесь только
                # переходим к следующему клиенту, ошибка одного не должна
                # останавливать опрос остальных.
                db.rollback()
            except Exception:  # noqa: BLE001 — фоновый цикл не должен падать целиком
                db.rollback()
                logger.exception(
                    "Фоновый синк WB клиента «%s» (id=%s) упал неожиданно", client.name, client.id
                )
    finally:
        db.close()


def _loop(interval: int) -> None:
    while not _stop_event.wait(interval):
        try:
            run_sync_cycle()
        except Exception:  # noqa: BLE001
            logger.exception("Фоновый цикл синка WB упал целиком")


def start() -> None:
    """Запускается из lifespan FastAPI (main.py). interval <= 0 — опрос выключен."""
    global _thread
    interval = get_settings().wb_sync_interval_sec
    if interval <= 0 or _thread is not None:
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), daemon=True, name="wb-sync")
    _thread.start()


def stop() -> None:
    global _thread
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
        _thread = None
