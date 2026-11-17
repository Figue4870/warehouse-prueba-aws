"""
Plataforma CENTRAL (Fase 7) — lado de CONSULTA (CQRS).

Las bodegas siguen siendo dueñas de su inventario (lado de comandos: cada una
con su PostgreSQL). La central NO consulta las bases de las bodegas: construye
su propio MODELO DE LECTURA a partir de los eventos que le llegan por RabbitMQ.

  api.py                -> API central: inventario global, estado de bodegas, sincronización, reportes
  projection.py         -> cómo cada evento actualiza el modelo de lectura (idempotente)
  sync.py               -> recuperación de huecos y resincronización con las bodegas
  workers/projector.py  -> consume central.projection y aplica los eventos
  workers/reconciler.py -> vigila heartbeats y secuencias; pide lo que falte
"""
