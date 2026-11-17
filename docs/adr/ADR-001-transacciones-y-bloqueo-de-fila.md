# ADR-001 — Transacción única y bloqueo de fila para los movimientos de inventario

## Contexto

Cada movimiento (entrada, salida, ajuste, reserva…) cambia el saldo en `inventory` y deja un registro en
`inventory_movements`. Si solo se guardara una de las dos cosas, el historial y el saldo dejarían de
coincidir. Además, dos salidas del mismo producto pueden llegar al mismo tiempo y la regla del negocio
dice que el inventario **nunca** puede quedar negativo.

## Alternativas

1. Actualizar saldo e historial en operaciones separadas (dos commits).
2. Una sola transacción sin bloqueo (leer saldo → calcular → escribir).
3. Una sola transacción con `SELECT ... FOR UPDATE` (bloqueo pesimista de la fila).
4. Bloqueo optimista (columna `version` y reintentar si cambió).

## Decisión

Opción 3. Todo el movimiento (saldo + historial + alerta) ocurre en una transacción y la fila del
inventario se bloquea con `FOR UPDATE` antes de leer el saldo. Además, PostgreSQL tiene
`CHECK (quantity >= 0)` como última línea de defensa.

## Consecuencias

- Correcto bajo concurrencia: prueba con 25 salidas simultáneas sobre 10 unidades → exactamente 10 aceptadas.
  Sin el bloqueo se aceptaron 22, 23 y 13 en tres corridas (se "vendió" lo que no había).
- Las escrituras sobre el **mismo** producto se atienden de una en una (esperan el bloqueo). Para
  productos distintos no hay espera. Si un producto tuviera muchísimas escrituras por segundo, el
  bloqueo optimista o colas por producto serían alternativas a evaluar.
