"""Состояние внешних интеграций — курсоры инкрементальных выгрузок и т.п.

Одна строка на ключ (например 'wb.product_cards'). cursor — произвольный JSON,
для WB это {updatedAt, nmID} последнего ответа предыдущей выгрузки.
"""

import datetime as dt

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from fulfil.db import Base


class IntegrationState(Base):
    __tablename__ = "integration_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
