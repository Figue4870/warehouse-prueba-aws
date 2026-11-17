-- Ventas (salidas SHIPMENT) por día y por bodega, últimos ${days} días.
-- DISTINCT: si un lote se exportó dos veces (por ejemplo, tras reconstruir la base central),
-- cada movimiento (event_id) se cuenta UNA sola vez.
WITH ventas AS (
    SELECT DISTINCT event_id, warehouse_id, quantity, unit_retail_price, occurred_at
    FROM "${database}"."movements"
    WHERE type = 'SHIPMENT'
      AND dt >= CAST(date_add('day', -${days}, current_date) AS varchar)
)
SELECT
    date(from_iso8601_timestamp(occurred_at))                                        AS dia,
    warehouse_id                                                                      AS bodega,
    sum(quantity)                                                                     AS unidades,
    round(sum(quantity * CAST(coalesce(unit_retail_price, '0') AS decimal(14, 2))), 2) AS valor_ventas
FROM ventas
GROUP BY 1, 2
ORDER BY 1 DESC, 2
