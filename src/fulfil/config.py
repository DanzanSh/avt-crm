from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Обязательные — приложение не стартует, если не заданы в окружении / .env
    # (пустая строка тоже считается «не задано»).
    database_url: str = Field(min_length=1)
    # С появления учётных записей (problems.txt, п.5) вход идёт по таблице users.
    # Эти два поля нужны только для первого запуска: миграция c7d2a9e5f013 и
    # страховочный сев в main.lifespan заводят из них владельца (хоста).
    admin_login: str = Field(min_length=1)
    admin_password: str = Field(min_length=1)

    # Обязателен, без дефолта: приложение со «change-me-in-production» подписывало бы
    # токены хоста публично известным ключом. openssl rand -hex 32 для генерации.
    jwt_secret: str = Field(min_length=32)
    jwt_expire_minutes: int = 60

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_not_trivial(cls, v: str) -> str:
        if v.lower().startswith("change-me") or len(set(v)) <= 1:
            raise ValueError(
                "JWT_SECRET похож на заглушку по умолчанию или состоит из одного "
                "повторяющегося символа. Сгенерируйте настоящий секрет: "
                "openssl rand -hex 32"
            )
        return v

    # development — локальный запуск и тесты (docs/redoc открыты, HSTS не ставится);
    # production — /docs, /redoc, /openapi.json отключены, добавляется HSTS.
    app_env: Literal["development", "production"] = "development"

    # Лимит на загрузку файлов (receiving/plan/import) — байт. Читается в два приёма:
    # заявленный размер и фактический (заголовку Content-Length не доверяем).
    max_upload_bytes: int = 5 * 1024 * 1024

    # Rate limit на /auth/login (в памяти процесса — деплой одна реплика, см.
    # fulfil.ratelimit). При масштабировании на несколько реплик нужен общий бэкенд
    # (Redis), сейчас не заводим намеренно.
    login_rate_limit: int = 10
    login_rate_window_sec: int = 300

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
