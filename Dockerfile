FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_DIR=/app/web

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY web ./web

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "swap.api:app", "--host", "0.0.0.0", "--port", "8000"]
