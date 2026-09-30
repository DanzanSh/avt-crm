from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from fulfil import jobs
from fulfil.api.v1 import (
    audit, auth, clients, dashboard, fbs, products, receiving, scan, settings_, stock, storage, users,
)
from fulfil.config import get_settings
from fulfil.db import SessionLocal
from fulfil.errors import AppError, app_error_handler
from fulfil.services import users as users_service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Страховочный сев хоста (problems.txt, п.5): на чистой БД без учёток войти
    # было бы невозможно — заводим владельца из ADMIN_LOGIN/ADMIN_PASSWORD.
    db = SessionLocal()
    try:
        users_service.ensure_host(db)
    finally:
        db.close()
    # Фоновый опрос WB (Этап 3 плана №3, п.3.1) — заказы + их статусы по каждому
    # клиенту. WB_SYNC_INTERVAL_SEC=0 отключает его совсем.
    jobs.start()
    try:
        yield
    finally:
        jobs.stop()


_settings = get_settings()
_prod = _settings.app_env == "production"

app = FastAPI(
    title="Fulfil PoC",
    lifespan=lifespan,
    # /docs, /redoc, /openapi.json открыты только в dev — в проде это неавторизованный
    # обзор всего API (план «безопасность», п.5).
    docs_url=None if _prod else "/docs",
    redoc_url=None if _prod else "/redoc",
    openapi_url=None if _prod else "/openapi.json",
)
app.add_exception_handler(AppError, app_error_handler)


@app.middleware("http")
async def no_stale_static(request: Request, call_next):
    """StaticFiles отдаёт web/ с Last-Modified, но без Cache-Control — браузер тогда
    кеширует JS эвристически и после обновления кода показывает страницу со старым
    core.js (так «Пользователи» зависали на «Загрузка…»: Fulfil.me ещё не было).
    no-cache — не «не кешировать», а «сверяй ETag перед использованием»: 304 дёшев."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# CSP допускает инлайн-стили и инлайн-скрипты/onerror= (фронт активно их использует,
# например web/products.html) — 'unsafe-inline' в style-src/script-src временно
# оставлен, иначе страницы перестанут работать. Уборка инлайна — отдельная задача.
# CORS сознательно не добавляем: фронт отдаётся тем же приложением (StaticFiles на
# "/" ниже), кросс-доменных запросов нет.
_CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), payment=()"
    response.headers["Content-Security-Policy"] = _CSP
    if _prod:
        # Только в проде — иначе локальный HTTP-доступ ломается после первого захода.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

app.include_router(auth.router, prefix="/api/v1")
app.include_router(settings_.router, prefix="/api/v1")
app.include_router(storage.router, prefix="/api/v1")
app.include_router(clients.router, prefix="/api/v1")
app.include_router(dashboard.router, prefix="/api/v1")
app.include_router(products.router, prefix="/api/v1")
app.include_router(receiving.router, prefix="/api/v1")
app.include_router(stock.router, prefix="/api/v1")
app.include_router(fbs.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health() -> dict:
    return {"ok": True}


_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
