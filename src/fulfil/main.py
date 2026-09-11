from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fulfil.api.v1 import audit, auth, clients, fbs, products, receiving, scan, settings_, stock, storage
from fulfil.errors import AppError, app_error_handler

app = FastAPI(title="Fulfil PoC")
app.add_exception_handler(AppError, app_error_handler)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(settings_.router, prefix="/api/v1")
app.include_router(storage.router, prefix="/api/v1")
app.include_router(clients.router, prefix="/api/v1")
app.include_router(products.router, prefix="/api/v1")
app.include_router(receiving.router, prefix="/api/v1")
app.include_router(stock.router, prefix="/api/v1")
app.include_router(fbs.router, prefix="/api/v1")
app.include_router(scan.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health() -> dict:
    return {"ok": True}


_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
