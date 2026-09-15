from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Обязательные — приложение не стартует, если не заданы в окружении / .env
    # (пустая строка тоже считается «не задано»).
    database_url: str = Field(min_length=1)
    admin_login: str = Field(min_length=1)
    admin_password: str = Field(min_length=1)

    jwt_secret: str = "change-me-in-production"
    jwt_expire_minutes: int = 60

    wb_mode: Literal["http", "mock"] = "mock"
    # Устарело — с Этапа 1 у каждого клиента свой ключ (clients.wb_api_key_enc) и свой
    # склад (clients.wb_warehouse_id). Эти два поля читает только миграция стадии 1,
    # чтобы завести клиента "Основной кабинет" из уже настроенного .env.
    wb_api_token: str = ""
    wb_warehouse_id: str = ""
    wb_api_base: str = "https://marketplace-api.wildberries.ru"
    # Карточки товаров живут на отдельном хосте content-api, не на marketplace-api.
    wb_content_api_base: str = "https://content-api.wildberries.ru"

    # Ключ шифрования API-ключей клиентов (Fernet, urlsafe-base64, 32 байта).
    # Пусто — сохранение ключа клиента невозможно, см. fulfil.secrets.
    client_secrets_key: str = ""
    # Имя клиента, которое миграция стадии 1 даёт "старому" однокабинетному клиенту.
    default_client_name: str = "Основной кабинет"

    # Период фонового опроса WB (заказы + их статусы) по каждому клиенту, секунды
    # (Этап 3 плана №3, п.3.1). 0 — фоновый опрос выключен, синк только по кнопке.
    wb_sync_interval_sec: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
