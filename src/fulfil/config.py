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
    wb_api_token: str = ""
    wb_api_base: str = "https://marketplace-api.wildberries.ru"
    # Карточки товаров живут на отдельном хосте content-api, не на marketplace-api.
    wb_content_api_base: str = "https://content-api.wildberries.ru"
    wb_warehouse_id: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
