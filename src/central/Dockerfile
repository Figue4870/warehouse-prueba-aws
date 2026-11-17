# Contexto de build: la carpeta src/ (para copiar la librería compartida src/common)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/common

WORKDIR /app

COPY central/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/platform_common /opt/common/platform_common
COPY central/central ./central

EXPOSE 8000

# La API central. Los workers usan la misma imagen:
#   python -m central.workers.projector | central.workers.reconciler
CMD ["uvicorn", "central.api:app", "--host", "0.0.0.0", "--port", "8000"]
