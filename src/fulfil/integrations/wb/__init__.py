from fulfil.config import get_settings
from fulfil.integrations.wb.base import WBClient
from fulfil.integrations.wb.http import WBHttpClient
from fulfil.integrations.wb.mock import WBMockClient

_client: WBClient | None = None


def get_wb_client() -> WBClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = WBHttpClient() if settings.wb_mode == "http" else WBMockClient()
    return _client


__all__ = ["WBClient", "WBHttpClient", "WBMockClient", "get_wb_client"]
