from fulfil.config import get_settings
from fulfil.integrations.wb.base import WBClient
from fulfil.integrations.wb.http import WBHttpClient
from fulfil.integrations.wb.mock import WBMockClient
from fulfil.models.client import Client
from fulfil.secrets import decrypt_api_key

# Мок хранит состояние (заказы/поставки) внутри процесса — один экземпляр на клиента,
# переиспользуемый между вызовами (иначе get_new_orders() каждый раз "видел" бы фикстуру
# заново). Боевой клиент состояния не хранит — новый экземпляр на каждый вызов дешевле,
# чем городить пул, и не тянет протухший токен между запросами.
_mock_clients: dict[int, WBMockClient] = {}


def get_wb_client(client: Client) -> WBClient:
    """Клиент WB API конкретного кабинета (Этап 1) — токен и client_id больше не
    глобальные: get_wb_client(client) вместо синглтона get_wb_client()."""
    settings = get_settings()
    if settings.wb_mode == "mock":
        if client.id not in _mock_clients:
            _mock_clients[client.id] = WBMockClient(client_id=client.id)
        return _mock_clients[client.id]

    token = decrypt_api_key(client.wb_api_key_enc) if client.wb_api_key_enc else ""
    return WBHttpClient(token, client_id=client.id, client_name=client.name)


__all__ = ["WBClient", "WBHttpClient", "WBMockClient", "get_wb_client"]
