"""Шифрование API-ключей клиентов WB (Этап 1, п.1.1) + разбор JWT WB без проверки подписи.

Ключ шифрования — CLIENT_SECRETS_KEY (Fernet, urlsafe-base64 32 байта). Он общий на всё
приложение и хранится ТОЛЬКО в окружении — если его потерять, все сохранённые ключи
клиентов придётся ввести заново (см. deploying-with-docker-compose.md).

API никогда не отдаёт расшифрованный ключ наружу — только hasApiKey/маску/дату истечения
(см. schemas/client.py). exp читается из payload JWT БЕЗ проверки подписи: нам не важно,
валиден ли токен криптографически, только когда он истекает по собственным утверждениям.
"""

import base64
import datetime as dt
import json

from cryptography.fernet import Fernet, InvalidToken

from fulfil.config import get_settings
from fulfil.errors import AppError

_GENERATE_KEY_HINT = (
    'Сгенерируйте ключ шифрования командой '
    '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
    'и пропишите его в .env как CLIENT_SECRETS_KEY, затем перезапустите сервер.'
)


def _fernet() -> Fernet:
    key = get_settings().client_secrets_key
    if not key:
        raise AppError(
            "Не задан ключ шифрования (CLIENT_SECRETS_KEY) — сохранить API-ключ клиента невозможно.",
            status_code=400,
            reason_code="no_secrets_key",
            what_to_do=_GENERATE_KEY_HINT,
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise AppError(
            "CLIENT_SECRETS_KEY некорректен — ожидается ключ Fernet (urlsafe-base64, 32 байта).",
            status_code=400,
            reason_code="invalid_secrets_key",
            what_to_do=_GENERATE_KEY_HINT,
        ) from exc


def encrypt_api_key(raw: str) -> str:
    """Шифрует ключ API клиента. Бросает AppError, если CLIENT_SECRETS_KEY не задан."""
    return _fernet().encrypt(raw.encode()).decode()


def decrypt_api_key(enc: str) -> str:
    """Расшифровывает ключ API клиента. При потере/смене CLIENT_SECRETS_KEY — WbApiError
    поднимает вызывающая сторона (integrations/wb), здесь — просто AppError 400."""
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken as exc:
        raise AppError(
            "Не удалось расшифровать сохранённый ключ API — похоже, изменился CLIENT_SECRETS_KEY.",
            status_code=400,
            reason_code="secrets_key_mismatch",
            what_to_do="Введите API-ключ клиента заново в разделе «Клиенты».",
        ) from exc


def mask_api_key(raw: str) -> str:
    """«…a1b2» — последние 4 символа, остальное скрыто. Короче 4 символов — маскируем целиком."""
    if len(raw) <= 4:
        return "…" + "•" * len(raw)
    return "…" + raw[-4:]


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def jwt_expires_at(token: str) -> dt.datetime | None:
    """Достаёт `exp` из payload JWT БЕЗ проверки подписи — нам важна дата истечения,
    заявленная самим токеном, не факт его криптографической валидности (сервер, который
    проверяет подпись, — сам WB). Не JWT (или без exp) -> None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(exp, tz=dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
