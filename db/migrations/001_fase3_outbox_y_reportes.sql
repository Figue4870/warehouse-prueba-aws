-- =====================================================================
-- Migración Fase 3: para una base de datos creada con el esquema de la
-- Fase 1-2 (sin perder datos). Si la base es nueva, NO hace falta:
-- schema.sql ya trae todo.
--
--   docker exec db_warehouse_WH-001 psql -U postgres -d warehouse_db -f /db/migrations/001_fase3_outbox_y_reportes.sql
-- =====================================================================
BEGIN;

ALTER TABLE products ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE inventory ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inventory_alerts ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE inventory_alerts ADD COLUMN IF NOT EXISTS notification_channel VARCHAR(40);

CREATE TABLE IF NOT EXISTS outbox_events (
    id              BIGSERIAL    PRIMARY KEY,
    event_id        VARCHAR(36)  NOT NULL,
    event_type      VARCHAR(60)  NOT NULL,
    event_version   SMALLINT     NOT NULL DEFAULT 1,
    routing_key     VARCHAR(120) NOT NULL,
    aggregate_type  VARCHAR(40)  NOT NULL,
    aggregate_id    VARCHAR(80)  NOT NULL,
    payload         JSONB        NOT NULL,
    correlation_id  VARCHAR(80),
    occurred_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sequence        BIGINT,
    published_at    TIMESTAMPTZ,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    last_error      TEXT,
    CONSTRAINT uq_outbox_event_id UNIQUE (event_id),
    CONSTRAINT uq_outbox_sequence UNIQUE (sequence)
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox_events (id) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS report_jobs (
    id              VARCHAR(40)  PRIMARY KEY,
    warehouse_id    VARCHAR(20)  NOT NULL,
    report_type     VARCHAR(40)  NOT NULL,
    params          JSONB        NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    result          JSONB,
    error           TEXT,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    correlation_id  VARCHAR(80),
    requested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    CONSTRAINT ck_report_jobs_status CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'))
);

COMMIT;
