from fulfil.schemas.common import CamelModel


class ScanResolveRequest(CamelModel):
    code: str
    # Не передан — код ищется среди всех клиентов; неоднозначность (баркод есть
    # у нескольких) → 409 со списком кандидатов (Этап 1, п.1.4).
    client_id: int | None = None
