# ADR-003 — Borrado lógico de productos

## Contexto

El README pide `DELETE /products/{id}`. El PDF exige un historial de transacciones para reportes. Si se
borra físicamente un producto, sus movimientos quedan huérfanos o hay que borrarlos también.

## Alternativas

1. Borrado físico en cascada (se pierde el historial).
2. Borrado físico solo si el producto nunca tuvo movimientos.
3. Borrado lógico: columna `is_active = FALSE`.

## Decisión

Opción 3, y solo si el producto no tiene unidades disponibles ni reservadas (si no, `409 Conflict`).
Un producto dado de baja no admite movimientos, no aparece en `GET /products` (salvo con
`?include_inactive=true`) y conserva todo su historial.

## Consecuencias

- El historial de compras/ventas queda intacto para reportes.
- El SKU sigue ocupado: no se puede crear otro producto con el mismo SKU (es el código universal).
- Las consultas de listado deben filtrar `is_active`.
