"""
Procesos que corren APARTE de la API (cada uno en su propio contenedor):

  python -m app.workers.relay                -> publica el outbox en RabbitMQ
  python -m app.workers.cache_worker         -> invalidación asíncrona del caché (Estrategia B)
  python -m app.workers.notification_worker  -> avisa al administrador de alertas
  python -m app.workers.report_worker        -> genera los reportes pedidos con POST /reports
  python -m app.workers.audit_worker         -> deja rastro (log de auditoría) de cada evento
"""
