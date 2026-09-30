"""Rate limiting в памяти процесса (план «безопасность», п.2).

Деплой — одна реплика (docker-compose.yml, сервис app), поэтому Redis/slowapi
избыточны: счётчик живёт в памяти процесса. Ограничение честно фиксируем здесь:
при рестарте счётчики обнуляются, на нескольких репликах лимит не общий — при
переходе на масштабирование сюда подставляется Redis-бэкенд.
"""

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    """Скользящее окно: не больше `limit` попыток на ключ за `window_sec` секунд."""

    def __init__(self, limit: int, window_sec: float):
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Регистрирует попытку и возвращает True, если лимит не превышен."""
        now = time.monotonic()
        cutoff = now - self.window_sec
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            self._gc(cutoff)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _gc(self, cutoff: float) -> None:
        """Лениво выбрасывает протухшие ключи, чтобы словарь не рос бесконечно.
        Вызывается изнутри hit(), уже под локом."""
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]
        for k in stale:
            del self._hits[k]
