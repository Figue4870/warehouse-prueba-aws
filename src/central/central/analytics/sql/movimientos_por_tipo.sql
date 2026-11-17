-- Cuántos movimientos de cada tipo hizo cada bodega, últimos ${days} días
WITH movimientos AS (
    SELECT DISTINCT event_id, warehouse_id, type, quantity
    FROM "${database}"."movements"
    WHERE dt >= CAST(date_add('day', -${days}, current_date) AS varchar)
)
SELECT
    warehouse_id   AS bodega,
    type           AS tipo,
    count(*)       AS movimientos,
    sum(quantity)  AS unidades
FROM movimientos
GROUP BY 1, 2
ORDER BY 1, 2
