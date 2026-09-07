FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости ставим первым слоем — пересборка образа при правках кода быстрее.
COPY pyproject.toml ./
COPY src/ ./src/
# -e (editable) обязателен: main.py вычисляет путь к статике как
# Path(__file__).resolve().parents[2] / "web". При обычном `pip install .`
# setuptools раскладывает пакет плоско в site-packages/fulfil/main.py, и
# parents[2] уезжает в .../lib/python3.12 вместо /app — статика (index.html,
# login.html и т.д.) перестаёт монтироваться, и всё отдаёт 404. С editable-
# установкой __file__ по-прежнему указывает на /app/src/fulfil/main.py,
# и parents[2] == /app, как и предполагает COPY web/ ./web/ ниже.
RUN pip install --upgrade pip && pip install -e .

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY web/ ./web/

RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "fulfil.main:app", "--host", "0.0.0.0", "--port", "8000"]
