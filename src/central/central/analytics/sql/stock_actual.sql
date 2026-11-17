-- Stock y valor del inventario por bodega según la ÚLTIMA foto exportada (busca fotos de los últimos ${days} días)
WITH ultima_foto AS (
    SELECT max(snapshot_at) AS snapshot_at
    FROM "${database}"."inventory"
    WHERE dt >= CAST(date_add('day', -${days}, current_date) AS varchar)
)
SELECT
    i.warehouse_id                                                                                       AS bodega,
    i.sku,
    i.product_name                                                                                       AS producto,
    i.quantity                                                                                           AS disponible,
    i.reserved_quantity                                                                                  AS reservado,
    round((i.quantity + i.reserved_quantity) * CAST(coalesce(i.retail_price, '0') AS decimal(14, 2)), 2) AS valor_venta,
    i.warehouse_status                                                                                   AS estado_bodega,
    i.snapshot_at                                                                                        AS foto_tomada
FROM "${database}"."inventory" i
JOIN ultima_foto u ON i.snapshot_at = u.snapshot_at
WHERE i.dt >= CAST(date_add('day', -${days}, current_date) AS varchar)
ORDER BY i.warehouse_id, i.sku
