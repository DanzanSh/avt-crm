FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости ставим первым слоем — пересборка образа при правках кода быстрее.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install .

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY web/ ./web/

# main.py ищет статику в parents[2]/"web" -> /app/web (совпадает с COPY выше).

RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "fulfil.main:app", "--host", "0.0.0.0", "--port", "8000"]
