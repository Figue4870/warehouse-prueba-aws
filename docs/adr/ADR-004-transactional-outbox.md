# ADR-004 — Transactional Outbox para publicar eventos sin pérdida

## Contexto

Desde la Fase 3, cada cambio de inventario debe generar un evento en RabbitMQ (caché, notificaciones,
reportes y, en la Fase 7, la central). Guardar en PostgreSQL y luego publicar en RabbitMQ son dos
escrituras en sistemas distintos: no pueden ser atómicas. Si RabbitMQ falla entre las dos, el evento
se pierde ("doble escritura").

## Alternativas

1. Publicar en RabbitMQ dentro de la petición, después del COMMIT (se pierde si RabbitMQ está caído).
2. Publicar antes del COMMIT (si el COMMIT falla, se anuncia algo que no ocurrió).
3. Transacciones distribuidas (2PC): RabbitMQ no las soporta y acoplan la API al broker.
4. **Transactional Outbox:** guardar el evento en una tabla de la misma base y en la misma transacción;
   un proceso aparte (relay) lo publica con publisher confirms.
5. Change Data Capture (Debezium) leyendo el log de PostgreSQL: potente, pero más infraestructura.

## Decisión

Opción 4. Tabla `outbox_events` + relay con publisher confirms, secuencia sin huecos por bodega,
advisory lock (un solo relay activo) y consumidores idempotentes. Los mensajes van a colas durables
con reintentos y DLQ.

## Consecuencias

- La API no depende de RabbitMQ: si el broker cae, la bodega sigue operando y los eventos esperan.
- Entrega **al menos una vez**: un evento puede llegar repetido, así que todo consumidor debe ser idempotente.
- Hay una pequeña demora (≈0,5 s de sondeo) entre el COMMIT y la publicación.
- La tabla crece: se limpian los eventos publicados con más de 30 días (nunca los pendientes).
