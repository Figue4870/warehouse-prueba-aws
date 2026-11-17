-- =====================================================================
-- Base de datos de la PLATAFORMA CENTRAL (Fase 7): MODELO DE LECTURA (CQRS)
--
-- No es una copia de las bases de las bodegas: es una vista optimizada para
-- consultas globales, construida a partir de sus EVENTOS. Todo lo de aquí se
-- puede reconstruir (snapshots + /sync/events), por eso la central puede caerse
-- o perder su base sin que se pierda información de negocio.
--
-- EXCEPCIÓN (Fase 6): las tablas transfers, transfer_history y transfer_commands
-- son el estado de la Saga que orquesta la central (al final del archivo).
-- Esas sí son datos propios de la central y se respaldan.
-- =====================================================================
BEGIN;

-- Bodegas conocidas y qué tan al día está la central con cada una
CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_id           VARCHAR(20)  PRIMARY KEY,
    api_url                VARCHAR(200),
    first_seen_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_heartbeat_at      TIMESTAMPTZ,
    last_event_at          TIMESTAMPTZ,
    reported_sequence      BIGINT       NOT NULL DEFAULT 0,  -- la bodega dice: publiqué hasta N
    max_received_sequence  BIGINT       NOT NULL DEFAULT 0,
    watermark_sequence     BIGINT       NOT NULL DEFAULT 0,  -- la central tiene TODO hasta aquí (sin huecos)
    watermark_updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    outbox_pending         INTEGER,
    last_snapshot_at       TIMESTAMPTZ,
    last_sync_at           TIMESTAMPTZ,
    last_sync_kind         VARCHAR(20),
    last_sync_status       VARCHAR(20),
    last_sync_error        TEXT
);

-- Catálogo global por SKU (código universal)
CREATE TABLE IF NOT EXISTS catalog_products (
    sku                  VARCHAR(64)  PRIMARY KEY,
    name                 VARCHAR(200) NOT NULL,
    description          TEXT,
    category             VARCHAR(100),
    unit                 VARCHAR(30),
    supplier             VARCHAR(200),
    supplier_cost        NUMERIC(14, 2),
    retail_price         NUMERIC(14, 2),
    wholesale_price      NUMERIC(14, 2),
    source_warehouse_id  VARCHAR(20),
    source_updated_at    TIMESTAMPTZ
);

-- Producto dentro de cada bodega (su id local cambia de una bodega a otra)
CREATE TABLE IF NOT EXISTS warehouse_products (
    warehouse_id      VARCHAR(20) NOT NULL,
    sku               VARCHAR(64) NOT NULL,
    product_id_local  INTEGER     NOT NULL,
    is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
    product_version   INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (warehouse_id, sku)
);

-- Saldo de cada SKU en cada bodega (solo se aplica un evento si su versión es más nueva)
CREATE TABLE IF NOT EXISTS warehouse_inventory (
    warehouse_id       VARCHAR(20) NOT NULL,
    sku                VARCHAR(64) NOT NULL,
    quantity           INTEGER     NOT NULL DEFAULT 0,
    reserved_quantity  INTEGER     NOT NULL DEFAULT 0,
    version            INTEGER     NOT NULL DEFAULT 0,
    source_updated_at  TIMESTAMPTZ,              -- cuándo cambió en la bodega
    projected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),  -- cuándo lo supo la central
    PRIMARY KEY (warehouse_id, sku)
);
CREATE INDEX IF NOT EXISTS ix_warehouse_inventory_sku ON warehouse_inventory (sku);

-- Historial global de movimientos (reportes y exportación a S3/Athena)
CREATE TABLE IF NOT EXISTS movement_history (
    event_id            VARCHAR(36)  PRIMARY KEY,
    warehouse_id        VARCHAR(20)  NOT NULL,
    sequence            BIGINT,
    movement_id         INTEGER,
    sku                 VARCHAR(64)  NOT NULL,
    product_name        VARCHAR(200),
    category            VARCHAR(100),
    type                VARCHAR(20)  NOT NULL,
    quantity            INTEGER      NOT NULL,
    quantity_after      INTEGER,
    reserved_after      INTEGER,
    unit_retail_price   NUMERIC(14, 2),
    unit_supplier_cost  NUMERIC(14, 2),
    lot                 VARCHAR(100),
    reference           VARCHAR(200),
    occurred_at         TIMESTAMPTZ  NOT NULL,
    projected_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    export_batch_id     VARCHAR(36)              -- lote con el que se exportó a S3 (Athena)
);
CREATE INDEX IF NOT EXISTS ix_movement_history_export ON movement_history (export_batch_id);
CREATE INDEX IF NOT EXISTS ix_movement_history_wh_time ON movement_history (warehouse_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_movement_history_sku ON movement_history (sku);

CREATE TABLE IF NOT EXISTS central_alerts (
    event_id      VARCHAR(36)  PRIMARY KEY,
    warehouse_id  VARCHAR(20)  NOT NULL,
    alert_type    VARCHAR(30)  NOT NULL,
    sku           VARCHAR(64),
    product_name  VARCHAR(200),
    message       TEXT,
    occurred_at   TIMESTAMPTZ  NOT NULL
);

-- IDEMPOTENCIA: cada evento se aplica una sola vez (llegue por RabbitMQ o por /sync)
CREATE TABLE IF NOT EXISTS processed_events (
    event_id      VARCHAR(36)  PRIMARY KEY,
    warehouse_id  VARCHAR(20)  NOT NULL,
    sequence      BIGINT,
    event_type    VARCHAR(60)  NOT NULL,
    source        VARCHAR(10)  NOT NULL,      -- stream | pull
    processed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_processed_warehouse_sequence UNIQUE (warehouse_id, sequence)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              SERIAL       PRIMARY KEY,
    warehouse_id    VARCHAR(20)  NOT NULL,
    kind            VARCHAR(20)  NOT NULL,    -- CATCH_UP | SNAPSHOT
    status          VARCHAR(20)  NOT NULL,    -- OK | ERROR
    events_applied  INTEGER      NOT NULL DEFAULT 0,
    detail          TEXT,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

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
