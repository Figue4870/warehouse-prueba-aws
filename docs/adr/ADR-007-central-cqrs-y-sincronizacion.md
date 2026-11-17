# ADR-007 — Central con CQRS, store-and-forward y reconciliación por secuencias

## Contexto

La central debe responder el inventario global (`GET /global/inventory/{sku}`) y reportes de toda la
empresa. Requisitos del proyecto: **no perder información**, que las bodegas sigan operando si la
central se cae, que la información llegue cuando vuelva, y que una bodega caída no tumbe la central.

## Alternativas

1. **Consulta en vivo (A):** la central pregunta a cada bodega en cada request.
2. **Base compartida:** todas las bodegas escriben en una base central. Rompe la autonomía de la
   Fase 4: si la base central cae, ninguna bodega opera.
3. **Modelo de lectura alimentado por eventos (B, CQRS)**, con mecanismos para no perder eventos.

## Decisión

Opción 3, con estas piezas:

- **Store-and-forward:** cola durable local `wh.central-forward` + `central_forwarder` que hace ack
  local solo después del publisher confirm del RabbitMQ central. Los eventos ya venían de un outbox
  transaccional (ADR-004).
- **Proyección idempotente** (`processed_events` por `event_id`) y **versionada** (saldo absoluto +
  `inventory_version`: un dato viejo nunca pisa uno nuevo).
- **Watermark por bodega** sobre la secuencia sin huecos del outbox y **reconciliador** que pide lo
  que falte por HTTP (`/sync/events`) o una foto completa (`/sync/snapshot`).
- **Heartbeats** con expiración para marcar bodegas `OFFLINE` y avisar datos posiblemente viejos.
- La consulta en vivo (A) se conserva en `/global/inventory/{sku}/live` como diagnóstico y para el
  experimento de la guía.

## Consecuencias

- La central responde en ~6 ms aunque haya bodegas caídas (A: ~40 ms e incompleta).
- **Consistencia eventual:** la respuesta puede ir segundos atrás; se informa con `stale`,
  `warnings` y `known_by_central_at`.
- Más piezas que operar (forwarder, proyector, reconciliador) y más pruebas.
- La base central es **reconstruible** desde las bodegas: se puede borrar sin perder información
  (dentro de la retención del outbox, 30 días; lo anterior se cubre con el snapshot de saldos).
- La central necesita poder llamar a las bodegas (`/sync`): en AWS se abre solo desde su security
  group y por la red privada.
