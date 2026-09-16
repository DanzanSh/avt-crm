from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fulfil.auth import get_current_user
from fulfil.db import get_db
from fulfil.services import dashboard as dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_user)])


@router.get("/summary")
def get_summary(client_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    return dashboard_service.get_dashboard_summary(db, client_id=client_id)
