# ADR-008 — Transferencias con una Saga orquestada por la central

## Contexto

Una transferencia toca dos bases de datos independientes (origen y destino). No hay una transacción
SQL común y las bodegas deben seguir siendo autónomas (Fase 4). Requisito del proyecto: nunca perder
ni duplicar unidades, aunque se caiga una bodega, la central o la red.

## Alternativas

1. **Transacción distribuida (2PC):** bloquea ambas bases y exige que todo esté arriba a la vez.
2. **Saga por coreografía:** cada bodega reacciona a los eventos de la otra. Sin coordinador, pero la
   lógica queda repartida y es difícil saber en qué va una transferencia.
3. **Saga orquestada por la central** con comandos y respuestas.

## Decisión

Opción 3:

- Orden de pasos **reservar → recibir → despachar**: las unidades quedan reservadas en el origen hasta
  que el destino confirma; la única compensación necesaria es liberar la reserva.
- Comandos en un **outbox de la central** (`transfer_commands`) y respuestas en el **outbox de cada
  bodega**, en la misma transacción que el cambio: nada se pierde si un proceso se cae.
- Una cola de comandos por bodega en el RabbitMQ central (durable).
- Idempotencia en la bodega con `(transfer_id, paso)` único, y en la central con `event_id` único.
- Plazos con `deadline` en los comandos (la bodega rechaza lo vencido) y compensación **nunca a
  ciegas**: si el destino no responde se le pide `CANCEL_RECEIVE` y se espera su respuesta.
- `TRANSFER_IN`/`TRANSFER_OUT` manuales bloqueados en la API de la bodega (409).

## Consecuencias

- Una transferencia es **eventualmente consistente**: durante segundos las unidades están reservadas.
- Si el destino queda caído mucho tiempo, la transferencia espera en `COMPENSATING` con las unidades
  reservadas (seguridad antes que rapidez). Se ve en `/transfers` y en el tablero.
- La central guarda estado propio (transferencias): a diferencia del modelo de lectura, esas tablas no
  se reconstruyen desde las bodegas y deben respaldarse.
- Más piezas: `command_worker` en cada bodega, `central_saga` y `central_command_relay` en la central.
