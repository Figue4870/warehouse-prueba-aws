-- =====================================================================
-- Migración de la CENTRAL a la Fase 6 (Saga) + exportación para Athena,
-- para una base central creada con la Fase 7. Si es nueva, NO hace falta.
--
--   docker exec central_postgres psql -U postgres -d central_db -f /db/migrations/central_002_saga_y_athena.sql
-- =====================================================================
BEGIN;

ALTER TABLE movement_history ADD COLUMN IF NOT EXISTS export_batch_id VARCHAR(36);
CREATE INDEX IF NOT EXISTS ix_movement_history_export ON movement_history (export_batch_id);

-- =====================================================================
-- Fase 6: SAGA de transferencias (estado propio de la central: NO se
-- reconstruye desde las bodegas; respáldese como cualquier base de negocio)
-- =====================================================================
CREATE TABLE IF NOT EXISTS transfers (
    id                        SERIAL        PRIMARY KEY,
    transfer_id               VARCHAR(30)   UNIQUE,          -- TR-000001
    idempotency_key           VARCHAR(100)  UNIQUE,
    request_hash              VARCHAR(64),
    sku                       VARCHAR(64)   NOT NULL,
    source_warehouse_id       VARCHAR(20)   NOT NULL,
    destination_warehouse_id  VARCHAR(20)   NOT NULL,
    quantity                  INTEGER       NOT NULL CHECK (quantity > 0),
    state                     VARCHAR(30)   NOT NULL,
    compensation_step         VARCHAR(20),
    failure_reason            TEXT,
    note                      TEXT,
    needs_review              BOOLEAN       NOT NULL DEFAULT false,
    timeout_seconds           INTEGER       NOT NULL,
    reserve_deadline          TIMESTAMPTZ,
    receive_deadline          TIMESTAMPTZ,
    simulate_failure          VARCHAR(30),
    correlation_id            VARCHAR(80),
    created_at                TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ   NOT NULL DEFAULT now(),
    finished_at               TIMESTAMPTZ,
    export_batch_id           VARCHAR(36),
    CONSTRAINT ck_transfers_state CHECK (state IN ('REQUESTED', 'RESERVING_SOURCE', 'SOURCE_RESERVED', 'IN_TRANSIT',
        'DESTINATION_RECEIVED', 'COMPLETED', 'FAILED', 'COMPENSATING', 'RESERVATION_RELEASED'))
);
CREATE INDEX IF NOT EXISTS ix_transfers_state ON transfers (state);
CREATE INDEX IF NOT EXISTS ix_transfers_export ON transfers (export_batch_id);

-- Cada cambio de estado; event_id único = una respuesta de bodega se procesa una sola vez
CREATE TABLE IF NOT EXISTS transfer_history (
    id            SERIAL        PRIMARY KEY,
    transfer_id   VARCHAR(30)   NOT NULL,
    from_state    VARCHAR(30),
    to_state      VARCHAR(30)   NOT NULL,
    event_id      VARCHAR(36)   UNIQUE,
    event_type    VARCHAR(60),
    warehouse_id  VARCHAR(20),
    detail        TEXT,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_transfer_history_transfer ON transfer_history (transfer_id);

-- OUTBOX de comandos de la central (los publica central_command_relay)
CREATE TABLE IF NOT EXISTS transfer_commands (
    id              SERIAL        PRIMARY KEY,
    command_id      VARCHAR(36)   NOT NULL UNIQUE,
    transfer_id     VARCHAR(30)   NOT NULL,
    warehouse_id    VARCHAR(20)   NOT NULL,
    command_type    VARCHAR(20)   NOT NULL,
    payload         JSONB         NOT NULL,
    deadline        TIMESTAMPTZ,
    correlation_id  VARCHAR(80),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ,
    attempts        INTEGER       NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_transfer_commands_transfer ON transfer_commands (transfer_id);
CREATE INDEX IF NOT EXISTS ix_transfer_commands_pending ON transfer_commands (id) WHERE published_at IS NULL;

-- Exportación a S3 para Athena (un lote = archivos con nombre fijo: re-subirlos no duplica)
CREATE TABLE IF NOT EXISTS export_batches (
    batch_id     VARCHAR(36)   PRIMARY KEY,
    dataset      VARCHAR(30)   NOT NULL,     -- movements | transfers | inventory
    row_count    INTEGER       NOT NULL DEFAULT 0,
    object_keys  JSONB,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    uploaded_at  TIMESTAMPTZ,
    last_error   TEXT
);

COMMIT;
