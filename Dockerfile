FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8050 \
    CRYSTALLM_PI_SHARED_DATA_DIR=/app/data \
    CRYSTALLM_PI_SHARED_OUTPUTS_DIR=/app/outputs \
    CRYSTALLM_PI_POLL_TIMEOUT_S=100

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data/uploads /app/outputs

EXPOSE 8050

CMD ["gunicorn", "--bind", "0.0.0.0:8050", "--workers", "1", "--worker-class", "gevent", "--timeout", "100", "app:server"]
