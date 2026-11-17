# Contexto de build: la carpeta src/ (para poder copiar la librería compartida src/common)
#   docker build -f src/backend/app/Dockerfile src
FROM python:3.11-slim

# Logs inmediatos (sin buffer) y sin archivos .pyc dentro del contenedor
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/common

WORKDIR /app

# Primero solo requirements: si no cambian, Docker reutiliza esta capa (build más rápido)
COPY backend/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Librería compartida (eventos + RabbitMQ confiable). Fase 3
COPY common/platform_common /opt/common/platform_common

COPY backend/app/app ./app
COPY backend/app/main.py main.py

EXPOSE 8000

# Por defecto arranca la API. Los workers usan la MISMA imagen con otro comando:
#   python -m app.workers.relay | cache_worker | notification_worker | report_worker | audit_worker
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
