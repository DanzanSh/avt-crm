"""Единый конверт ошибок (конвенция из DEV-PLAN.md, раздел «Конвенции API»).

Ошибка = ненулевой HTTP-статус, всегда. Тело — три поля: detail (текст человеку),
reason_code (машинный код), what_to_do (что делать прямо сейчас). В эталоне
сосуществовали два конверта и часть ручек отдавала 200+ok:false — здесь такого нет.
"""

from fastapi import Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        reason_code: str | None = None,
        what_to_do: str | None = None,
        extra: dict | None = None,
    ):
        self.detail = detail
        self.status_code = status_code
        self.reason_code = reason_code
        self.what_to_do = what_to_do
        self.extra = extra or {}
        super().__init__(detail)


class CellOccupiedError(AppError):
    """409 — ячейка занята другим SKU или не осталось места."""

    def __init__(self, detail: str, suggestions: list[dict] | None = None):
        super().__init__(
            detail,
            status_code=status.HTTP_409_CONFLICT,
            reason_code="cell_occupied",
            what_to_do="Отсканируйте другую ячейку из списка предложенных.",
            extra={"suggestions": suggestions or []},
        )


class CellBlockedError(AppError):
    """422 — ячейка заблокирована."""

    def __init__(self, detail: str, reason: str | None = None):
        super().__init__(
            detail,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            reason_code="cell_blocked",
            what_to_do="Выберите другую ячейку либо снимите блокировку в карте склада.",
            extra={"blockedReason": reason},
        )


class BarcodeNotAllowedError(AppError):
    """409 — баркод не входит в список разрешённых для ячейки."""

    def __init__(self, detail: str, allowed: list[str]):
        super().__init__(
            detail,
            status_code=status.HTTP_409_CONFLICT,
            reason_code="barcode_not_allowed",
            what_to_do="Разместите товар в ячейку, для которой он разрешён, "
            "либо измените допуск ячейки в настройках.",
            extra={"allowedBarcodes": allowed},
        )


class NotFoundError(AppError):
    def __init__(self, detail: str):
        super().__init__(
            detail,
            status_code=status.HTTP_404_NOT_FOUND,
            reason_code="not_found",
        )


class AmbiguousCodeError(AppError):
    """400 — сырой и исправленный по раскладке коды дают разные сущности."""

    def __init__(self, detail: str = "Код неоднозначен после исправления раскладки."):
        super().__init__(
            detail,
            status_code=status.HTTP_400_BAD_REQUEST,
            reason_code="ambiguous_code",
            what_to_do="Отсканируйте код ещё раз или введите вручную.",
        )


class DuplicateMarkError(AppError):
    """409 — код маркировки уже использован в другом заказе."""

    def __init__(self, detail: str = "Эта марка уже привязана к другому заказу."):
        super().__init__(
            detail,
            status_code=status.HTTP_409_CONFLICT,
            reason_code="mark_already_used",
            what_to_do="Проверьте товар — возможно, это чужая единица.",
        )


class CellsNotReleasableError(AppError):
    """409 — нельзя удалить/уменьшить зону-стеллаж-ячейку: что-то из набора занято/заблокировано."""

    def __init__(self, detail: str, blocking_cells: list[dict]):
        super().__init__(
            detail,
            status_code=status.HTTP_409_CONFLICT,
            reason_code="cells_not_releasable",
            what_to_do="Освободите ячейки — переместите товар или снимите блокировку — и повторите.",
            extra={"blockingCells": blocking_cells},
        )


class StockChangedError(AppError):
    """409 — CAS: остаток изменился с момента, когда его увидел оператор."""

    def __init__(self, detail: str, actual_qty: int):
        super().__init__(
            detail,
            status_code=status.HTTP_409_CONFLICT,
            reason_code="stock_changed",
            what_to_do="Остаток изменился — проверьте актуальное значение и повторите.",
            extra={"actualQty": actual_qty},
        )


class ZoneCodeNotLatinError(AppError):
    """400 — код зоны набран не латиницей (в т.ч. кириллица-гомоглиф)."""

    def __init__(self, raw: str, suggestion: str | None = None):
        detail = f'Код зоны «{raw}» должен быть на латинице.'
        if suggestion:
            detail += f' Вероятно, вы имели в виду «{suggestion}» (латиница).'
        super().__init__(
            detail,
            status_code=status.HTTP_400_BAD_REQUEST,
            reason_code="zone_code_not_latin",
            what_to_do="Введите код зоны латинскими буквами и цифрами (A, B, A1…).",
            extra={"suggestion": suggestion},
        )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    body = {"detail": exc.detail, "reasonCode": exc.reason_code, "whatToDo": exc.what_to_do}
    body.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=body)
