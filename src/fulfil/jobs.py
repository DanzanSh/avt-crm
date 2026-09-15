"""Фоновый опрос WB: заказы, их статусы, поставки и остатки FBS по каждому клиенту
(Этап 3 плана №3, п.3.1; поставки — Этап 4, п.4.2; остатки FBS — P0-3).

Статусы уже загруженных заказов раньше не обновлялись — опрашивался только
GET /orders/new, поэтому отмена покупателем и «в доставке»/«принята» до нас
не доходили. Здесь этот опрос выполняется по расписанию, а не только по клику
«Обновить из WB». То же самое для поставок: без периодического sync_supplies()
статус «Принята» (scanDt) узнаётся только по ручному клику на странице. И для
остатков FBS: без периодического refresh_wb_fbs_amounts() кэш Product.wb_fbs_amount
расходится с тем, что WB отдаёт при продажах (P0-3).

Каждый шаг клиента — отдельная попытка (P2-13): раньше все четыре шага были в
одном try/except, и если падал sync_orders_from_wb, для этого клиента в этом же
цикле не выполнялись ни опрос статусов, ни поставки, ни остатки — хотя ничего
общего с первой ошибкой у них нет.

uvicorn в этом проекте работает в одном процессе (см. Dockerfile) — фоновый
поток и process-local Event достаточны. При масштабировании на несколько
воркеров/процессов нужен pg_try_advisory_lock, иначе несколько процессов
будут гонять синк параллельно и упираться в лимиты WB быстрее, чем один.
"""

import logging
import threading

from fulfil.config import get_settings
from fulfil.db import SessionLocal
from fulfil.errors import AppError
from fulfil.integrations.wb import get_wb_client
from fulfil.services import stock as stock_service
from fulfil.services.clients import list_syncable_clients
from fulfil.services.orders import refresh_order_statuses, sync_orders_from_wb
from fulfil.services.supplies import sync_supplies

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_thread: threading.Thread | None = None

# Порядок значим: заказы -> их статусы -> поставки -> остатки FBS. Каждый шаг сам
# коммитит и сам пишет client.last_sync_at/last_sync_error при успехе/ошибке —
# здесь только изолируем шаги друг от друга.
_SYNC_STEPS = (
    sync_orders_from_wb,
    refresh_order_statuses,
    sync_supplies,
    stock_service.refresh_wb_fbs_amounts,
)


def run_sync_cycle() -> None:
    """Один проход по всем активным клиентам с ключом и складом — публичная
    функция, чтобы её можно было дёрнуть и вручную (не только из фонового потока)."""
    db = SessionLocal()
    try:
        for client in list_syncable_clients(db):
            wb_client = get_wb_client(client)
            for step in _SYNC_STEPS:
                try:
                    step(db, client, wb_client)
                except AppError:
                    # Шаг сам записал причину в client.last_sync_error и закоммитил —
                    # здесь только откатываем незакоммиченный хвост и переходим к
                    # следующему шагу/клиенту, не давая одной ошибке заблокировать
                    # остальные шаги этого же клиента.
                    db.rollback()
                except Exception:  # noqa: BLE001 — фоновый цикл не должен падать целиком
                    db.rollback()
                    logger.exception(
                        "Фоновый синк WB клиента «%s» (id=%s), шаг %s — упал неожиданно",
                        client.name, client.id, getattr(step, "__name__", step),
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
