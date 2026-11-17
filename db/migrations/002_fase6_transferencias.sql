-- =====================================================================
-- Migración Fase 6 (Saga de transferencias) para una bodega ya creada.
-- Si la base es nueva, NO hace falta: schema.sql ya trae todo.
--
--   docker exec db_warehouse_WH-001 psql -U postgres -d warehouse_db -f /db/migrations/002_fase6_transferencias.sql
-- =====================================================================
BEGIN;

-- Fase 6: lo que esta bodega hizo en cada paso de una transferencia (Saga).
-- (transfer_id, step) único = un mismo paso nunca se ejecuta dos veces.
CREATE TABLE IF NOT EXISTS transfer_operations (
    id              SERIAL       PRIMARY KEY,
    transfer_id     VARCHAR(30)  NOT NULL,
    step            VARCHAR(20)  NOT NULL,
    outcome         VARCHAR(20)  NOT NULL,
    sku             VARCHAR(64),
    product_id      INTEGER      REFERENCES products (id),
    quantity        INTEGER,
    reason          TEXT,
    movement_id     INTEGER      REFERENCES inventory_movements (id),
    command_id      VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_transfer_operations_step UNIQUE (transfer_id, step),
    CONSTRAINT ck_transfer_operations_step CHECK (step IN ('RESERVE', 'RECEIVE', 'COMMIT', 'RELEASE', 'CANCEL_RECEIVE')),
    CONSTRAINT ck_transfer_operations_outcome CHECK (outcome IN ('DONE', 'REJECTED', 'NOOP'))
);
CREATE INDEX IF NOT EXISTS ix_transfer_operations_transfer ON transfer_operations (transfer_id);

COMMIT;
