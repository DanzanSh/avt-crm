from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.schemas.product import ProductOut
from fulfil.schemas.scan import ScanResolveRequest
from fulfil.schemas.storage import CellOut
from fulfil.services.scan import resolve_scan

router = APIRouter(prefix="/scan", tags=["scan"], dependencies=[Depends(get_current_user)])


@router.post("/resolve")
def resolve(body: ScanResolveRequest, db: Session = Depends(get_db)) -> dict:
    """Классифицирует отсканированный код на сервере — фронт больше не решает по
    стадии экрана, что это (FEATURES-PLAN.md, этап 4.3)."""
    kind, entity = resolve_scan(db, body.code)
    if kind == "product":
        return {"kind": "product", "product": ProductOut.model_validate(entity).model_dump(by_alias=True)}
    return {"kind": "cell", "cell": CellOut.model_validate(entity).model_dump(by_alias=True)}
