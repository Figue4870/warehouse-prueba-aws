-- =====================================================================
-- Datos de ejemplo para una bodega (opcional).
--
-- Cómo cargarlo (con la bodega corriendo en Docker):
--   docker exec db_warehouse_WH-001 psql -U postgres -d warehouse_db -v wh=WH-001 -f /db/seed.sql
-- Para otra bodega cambia el contenedor y el valor de wh:
--   docker exec db_warehouse_WH-002 psql -U postgres -d warehouse_db -v wh=WH-002 -f /db/seed.sql
--
-- Se puede ejecutar varias veces: los productos que ya existen (mismo SKU)
-- se saltan y no se duplica inventario ni movimientos.
-- =====================================================================

-- Si no se pasó -v wh=..., se usa WH-001
\if :{?wh}
\else
  \set wh WH-001
\endif

BEGIN;

WITH new_products AS (
    INSERT INTO products (sku, name, description, category, unit, supplier, lot,
                          supplier_cost, retail_price, wholesale_price)
    VALUES
        ('EPP-CASCO-001',  'Casco de seguridad blanco', 'Casco dieléctrico clase E', 'EPP', 'UNIDAD',
         'Seguridad Industrial del Valle', 'L-2026-10-A', 18000, 32000, 26000),
        ('EPP-GUANTE-002', 'Guantes de nitrilo (caja x100)', 'Talla M', 'EPP', 'CAJA',
         'Distribuciones Médicas SAS', 'L-2026-09-B', 21000, 38000, 31000),
        ('HER-TALADRO-003', 'Taladro percutor 1/2"', '750 W, con maletín', 'Herramientas', 'UNIDAD',
         'Ferretería Mayorista Andina', 'L-2026-08-C', 180000, 289000, 245000),
        ('LOG-CINTA-004', 'Cinta de embalaje transparente', '48 mm x 100 m', 'Logística', 'ROLLO',
         'Empaques del Pacífico', 'L-2026-10-D', 3500, 7000, 5200),
        ('LOG-ESTIBA-005', 'Estiba plástica 120x100', 'Carga dinámica 1.500 kg', 'Logística', 'UNIDAD',
         'Plásticos Industriales Cali', 'L-2026-07-E', 95000, 150000, 128000)
    ON CONFLICT (sku) DO NOTHING
    RETURNING id, sku
),
initial_stock (sku, qty) AS (
    VALUES ('EPP-CASCO-001', 50), ('EPP-GUANTE-002', 120), ('HER-TALADRO-003', 8),
           ('LOG-CINTA-004', 300), ('LOG-ESTIBA-005', 0)
),
new_inventory AS (
    INSERT INTO inventory (product_id, warehouse_id, quantity, reserved_quantity)
    SELECT np.id, :'wh', s.qty, 0
    FROM new_products np
    JOIN initial_stock s USING (sku)
    RETURNING product_id, quantity
)
INSERT INTO inventory_movements (product_id, warehouse_id, type, quantity, quantity_after, reserved_after, reference)
SELECT product_id, :'wh', 'RECEIPT', quantity, quantity, 0, 'SEED-INVENTARIO-INICIAL'
FROM new_inventory
WHERE quantity > 0;

COMMIT;

SELECT p.id, p.sku, p.name, i.warehouse_id, i.quantity AS disponible, i.reserved_quantity AS reservado
FROM products p JOIN inventory i ON i.product_id = p.id
ORDER BY p.id;
