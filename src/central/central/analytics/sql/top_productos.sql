-- Los 10 productos más vendidos de la empresa (todas las bodegas), últimos ${days} días
WITH ventas AS (
    SELECT DISTINCT event_id, warehouse_id, sku, product_name, quantity, unit_retail_price
    FROM "${database}"."movements"
    WHERE type = 'SHIPMENT'
      AND dt >= CAST(date_add('day', -${days}, current_date) AS varchar)
)
SELECT
    sku,
    max(product_name)                                                                 AS producto,
    sum(quantity)                                                                     AS unidades_vendidas,
    round(sum(quantity * CAST(coalesce(unit_retail_price, '0') AS decimal(14, 2))), 2) AS valor_ventas,
    count(DISTINCT warehouse_id)                                                      AS bodegas
FROM ventas
GROUP BY sku
ORDER BY unidades_vendidas DESC
LIMIT 10
