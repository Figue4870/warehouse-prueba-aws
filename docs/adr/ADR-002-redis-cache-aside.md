# ADR-002 — Redis con Cache-Aside, TTL de 30 s e invalidación síncrona

## Contexto

`GET /inventory/{id}` se consulta muchas más veces de las que el inventario cambia (README: bodegas con
800 req/s de lectura). Leer PostgreSQL en cada consulta gasta conexiones, CPU y tiempo.

## Alternativas

1. Leer siempre PostgreSQL.
2. Caché en la memoria de cada proceso de la API.
3. Redis con Cache-Aside (la API consulta Redis y, si no está, PostgreSQL y lo guarda).
4. Redis con Write-Through (escribir en Redis cada vez que se escribe en PostgreSQL).

Para invalidar: A) borrar la clave síncronamente después del COMMIT; B) publicar un evento en RabbitMQ
y que un worker borre la clave (requiere la Fase 3).

## Decisión

Redis + Cache-Aside con clave `warehouse:{WAREHOUSE_ID}:inventory:{product_id}`, TTL de 30 s e
invalidación síncrona (A) **después** del COMMIT. Si Redis falla, la API lee PostgreSQL (BYPASS) y deja
de intentar Redis durante 5 s.

## Consecuencias

- En el experimento: 5.000 lecturas → 0 consultas SQL con caché vs 5.000 sin caché; 2,5–3× más
  requests por segundo con 20–50 usuarios concurrentes.
- Puede haber un dato viejo por una ventana corta (carrera lectura/escritura o invalidación fallida);
  el TTL lo limita a 30 s como máximo. Por eso las **escrituras** nunca leen de Redis: siempre leen
  PostgreSQL con bloqueo.
- Redis es una dependencia más que operar, pero no es crítica: si cae, la bodega sigue funcionando.
- La estrategia B (asíncrona) se podrá comparar cuando exista RabbitMQ (Fase 3).
