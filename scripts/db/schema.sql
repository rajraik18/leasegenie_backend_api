-- =====================================================================
-- LeaseGenie API — Postgres Schema (PostgreSQL 14+ with pgvector 0.7+)
-- =====================================================================
--
-- Idempotent: every CREATE uses IF NOT EXISTS. Safe to re-run.
--
-- Run via:
--   psql -h <host> -U <user> -d leasegenie -f schema.sql
--   OR
--   scripts/db.sh init      (uses Compose's postgres container)
--   OR
--   python -m scripts.db.manage init    (multi-backend, also seeds extension)
--
-- Hierarchy:
--   projects → properties → tenants → documents → field_values
--   tenants → field_overrides
--   tenants → audit_log
--   tenants → extraction_jobs
--   documents → clause_embeddings   (NEW: pgvector store, replaces ChromaDB)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Extensions — pgvector for embeddings, pgcrypto for gen_random_uuid()
-- ---------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------
-- 1. projects
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255)    NOT NULL,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- 2. properties
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS properties (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID            NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            VARCHAR(255)    NOT NULL,
    property_type   VARCHAR(32)     NOT NULL,
    address         VARCHAR(512),
    CONSTRAINT ck_properties_type CHECK (
        property_type IN ('Retail','Industrial','Office','Mixed-Use','Unknown')
    )
);
CREATE INDEX IF NOT EXISTS ix_properties_project ON properties(project_id);

-- ---------------------------------------------------------------------
-- 3. tenants
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     UUID            NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    name            VARCHAR(255)    NOT NULL,
    suite_number    VARCHAR(64),
    abstract_type   VARCHAR(64)     NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_tenants_property ON tenants(property_id);

-- ---------------------------------------------------------------------
-- 4. documents — base lease + up to 7 amendments per tenant
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename        VARCHAR(512)    NOT NULL,
    storage_path    VARCHAR(1024)   NOT NULL,
    document_type   VARCHAR(32)     NOT NULL,
    document_order  INTEGER         NOT NULL,
    effective_date  TIMESTAMPTZ,
    ocr_status      VARCHAR(32)     NOT NULL DEFAULT 'pending',
    uploaded_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_doc_order UNIQUE (tenant_id, document_order),
    CONSTRAINT ck_documents_type CHECK (
        document_type IN ('base_lease','amendment','sublease','guaranty','other')
    ),
    CONSTRAINT ck_documents_ocr_status CHECK (
        ocr_status IN ('pending','ocr_in_progress','extracting','complete','failed')
    )
);
CREATE INDEX IF NOT EXISTS ix_documents_tenant ON documents(tenant_id);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(ocr_status);

-- ---------------------------------------------------------------------
-- 5. field_values — extracted (tenant, document, field) tuples
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field_values (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id         UUID            NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_id            VARCHAR(128)    NOT NULL,
    value               TEXT,
    raw_value           TEXT,
    confidence          REAL            NOT NULL DEFAULT 0,
    page_number         INTEGER,
    clause_number       VARCHAR(64),
    clause_text         TEXT,
    question_answers    JSONB,
    needs_review        BOOLEAN         NOT NULL DEFAULT FALSE,
    red_flags           JSONB,
    extracted_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_fv_unique UNIQUE (tenant_id, document_id, field_id),
    CONSTRAINT ck_fv_confidence CHECK (confidence >= 0 AND confidence <= 1)
);
CREATE INDEX IF NOT EXISTS ix_fv_tenant_field ON field_values(tenant_id, field_id);
CREATE INDEX IF NOT EXISTS ix_fv_document ON field_values(document_id);
CREATE INDEX IF NOT EXISTS ix_fv_extracted_at ON field_values(extracted_at DESC);
CREATE INDEX IF NOT EXISTS ix_fv_needs_review ON field_values(tenant_id) WHERE needs_review = TRUE;

-- ---------------------------------------------------------------------
-- 6. field_overrides — manual overrides per (tenant, field)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS field_overrides (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    field_id    VARCHAR(128)    NOT NULL,
    value       TEXT,
    comment     TEXT,
    updated_by  VARCHAR(255),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_override_unique UNIQUE (tenant_id, field_id)
);
CREATE INDEX IF NOT EXISTS ix_fo_tenant ON field_overrides(tenant_id);

-- ---------------------------------------------------------------------
-- 7. audit_log — immutable trail of field changes
-- ---------------------------------------------------------------------
-- tenant_id is nullable so we can record global (non-tenant) events like
-- schema uploads, deployments, etc. Tenant-scoped events still cascade-
-- delete with the tenant.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID            REFERENCES tenants(id) ON DELETE CASCADE,
    field_id    VARCHAR(128)    NOT NULL,
    action      VARCHAR(32)     NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    actor       VARCHAR(255),
    "timestamp" TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_audit_action CHECK (action IN ('extract','override','revert'))
);
CREATE INDEX IF NOT EXISTS ix_audit_tenant_field
    ON audit_log(tenant_id, field_id, "timestamp" DESC);

-- ---------------------------------------------------------------------
-- 8. extraction_jobs — async job tracking for Celery
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_jobs (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    schema_id           VARCHAR(128),                                     -- null = built-in BRD playbooks
    schema_version      VARCHAR(32),
    status              VARCHAR(32)     NOT NULL DEFAULT 'queued',
    progress            INTEGER         NOT NULL DEFAULT 0,
    total_fields        INTEGER         NOT NULL DEFAULT 0,
    completed_fields    INTEGER         NOT NULL DEFAULT 0,
    error               TEXT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ej_status CHECK (
        status IN ('queued','running','complete','failed','cancelled')
    ),
    CONSTRAINT ck_ej_progress CHECK (progress >= 0 AND progress <= 100)
);
CREATE INDEX IF NOT EXISTS ix_ej_tenant_status ON extraction_jobs(tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_ej_status_created ON extraction_jobs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_ej_schema ON extraction_jobs(schema_id) WHERE schema_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- 9. extraction_schemas — user-uploaded JSON specs that override the BRD
-- ---------------------------------------------------------------------
-- Each schema_id can have multiple versions; latest is canonical. At most
-- one row per schema_id has is_active=TRUE (enforced at the app layer).
-- The schema_json column holds the full uploaded JSON document.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_schemas (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_id   VARCHAR(128)    NOT NULL,
    name        VARCHAR(255)    NOT NULL,
    version     VARCHAR(32)     NOT NULL DEFAULT '1.0.0',
    description TEXT,
    schema_json JSONB           NOT NULL,
    is_active   BOOLEAN         NOT NULL DEFAULT FALSE,
    created_by  VARCHAR(255),
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_schema_version UNIQUE (schema_id, version)
);
CREATE INDEX IF NOT EXISTS ix_schema_active ON extraction_schemas(is_active)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS ix_schema_slug ON extraction_schemas(schema_id);
CREATE INDEX IF NOT EXISTS ix_schema_json_gin ON extraction_schemas USING GIN (schema_json);

-- ---------------------------------------------------------------------
-- 9. clause_embeddings — pgvector store, replaces ChromaDB
-- ---------------------------------------------------------------------
-- Vector dimension matches OLLAMA_EMBED_DIM (default 768 for nomic-embed-text).
-- For BGE-large, use 1024. For OpenAI ada-002, use 1536. Change here AND in
-- app/config.py:ollama_embed_dim if you swap embedders — they must match.
--
-- Cosine distance is used because Ollama embeddings are not unit-normalized.
-- The HNSW index dramatically speeds up ANN queries at modest recall cost
-- (typical 95-98% recall@5 for our tile sizes).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clause_embeddings (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID            NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id       UUID            NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    page_number     INTEGER         NOT NULL,
    clause_number   VARCHAR(64),
    heading         VARCHAR(512),
    clause_text     TEXT            NOT NULL,
    embedding       VECTOR(768)     NOT NULL,
    char_start      INTEGER         NOT NULL,
    char_end        INTEGER         NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_clause_position UNIQUE (document_id, page_number, char_start)
);

-- HNSW index for fast cosine similarity search.
-- m=16, ef_construction=64 are reasonable defaults for ~10K-1M vectors.
CREATE INDEX IF NOT EXISTS ix_clause_emb_hnsw
    ON clause_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Partition scan helpers
CREATE INDEX IF NOT EXISTS ix_clause_emb_tenant ON clause_embeddings(tenant_id);
CREATE INDEX IF NOT EXISTS ix_clause_emb_document ON clause_embeddings(document_id);

-- ---------------------------------------------------------------------
-- Schema version sentinel
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     VARCHAR(32)     PRIMARY KEY,
    applied_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    applied_by  VARCHAR(255),
    notes       TEXT
);

INSERT INTO schema_version (version, applied_by, notes)
VALUES ('2.0.0', CURRENT_USER, 'Postgres + pgvector — 9 tables (Week 1-3 release)')
ON CONFLICT (version) DO NOTHING;

-- ---------------------------------------------------------------------
-- Final state report
-- ---------------------------------------------------------------------
\echo
\echo '====================================================='
\echo '  LeaseGenie schema initialization complete.'
\echo '====================================================='
\echo

SELECT
    schemaname,
    relname AS tablename,
    n_live_tup AS row_count
FROM pg_stat_user_tables
WHERE relname IN (
    'projects','properties','tenants','documents',
    'field_values','field_overrides','audit_log',
    'extraction_jobs','extraction_schemas','clause_embeddings','schema_version'
)
ORDER BY relname;
