# ADR-005 — Balanceador Nginx por bodega con 3 instancias de la API

## Contexto

La Fase 5 pide varias instancias de la API de una bodega, un balanceador con health checks y
demostrar que el sistema sigue atendiendo si una instancia se cae. Debe funcionar igual en el PC de
cada integrante y en una EC2 del Learner Lab.

## Alternativas

1. AWS Application Load Balancer: gestionado, pero solo existe en AWS (no se puede probar en local) y
   tiene costo por hora en el presupuesto del Learner Lab.
2. `uvicorn --workers 3` en un solo contenedor: no permite apagar una "instancia" ni verla por separado.
3. **Nginx como contenedor de la bodega, con `api_1..api_3` explícitas.**
4. Traefik o HAProxy: válidos, pero el equipo ya usaba Nginx en el frontend.

## Decisión

Opción 3: Nginx 1.27 con round-robin, `max_fails`/`fail_timeout` (salud pasiva),
`proxy_next_upstream` (reintento en otra instancia) y `resolve` (vuelve a resolver la IP de una
instancia que se reinicia). Redis, PostgreSQL y RabbitMQ siguen siendo uno por bodega, compartidos.

## Consecuencias

- Una instancia caída no produce errores visibles (520/520 en el experimento).
- Nginx no reintenta POST que ya llegaron a una API: no hay movimientos duplicados.
- El balanceador es un punto único dentro de la bodega (si cae Nginx, cae la API de esa bodega).
  En producción se pondrían dos balanceadores o un ALB.
- La cantidad de instancias es fija (3) en la plantilla. Cambiarla es agregar `api_4` y una línea
  en `lb.conf`.
