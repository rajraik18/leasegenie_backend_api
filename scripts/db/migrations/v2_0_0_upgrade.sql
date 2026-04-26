-- =====================================================================
-- LeaseGenie API — Upgrade migration to v2.0.0
-- =====================================================================
--
-- Run this on existing deployments (v1 with MSSQL+ChromaDB OR pre-fix v2)
-- to bring them up to the v2.0.0 schema. Idempotent — safe to re-run.
--
-- Apply via:
--   .\scripts\db.ps1 upgrade-sql           (runs every .sql in this dir in order)
--   OR
--   psql -f scripts\db\migrations\v2_0_0_upgrade.sql
--
-- For fresh installs, scripts\db\schema.sql is canonical and you do NOT
-- need this file — `.\scripts\db.ps1 init` applies the full schema.
--
-- This migration:
--   1. Ensures both extensions (pgcrypto, vector) are installed
--   2. Makes audit_log.tenant_id nullable (was NOT NULL in early drafts)
--   3. Creates the extraction_schemas table if missing
--   4. Adds schema_id + schema_version columns to extraction_jobs if missing
--   5. Creates the GIN index on extraction_schemas.schema_json
--   6. Creates the partial unique index on is_active
--   7. Creates the HNSW index on clause_embeddings.embedding if missing
--   8. Records the version in schema_version
-- =====================================================================

BEGIN;

-- 1. Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. audit_log.tenant_id nullable
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'audit_log'
          AND column_name = 'tenant_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE audit_log ALTER COLUMN tenant_id DROP NOT NULL;
        RAISE NOTICE 'audit_log.tenant_id: changed to nullable';
    ELSE
        RAISE NOTICE 'audit_log.tenant_id: already nullable (skipped)';
    END IF;
END $$;

-- 3. extraction_schemas table
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

-- 4. extraction_jobs.schema_id + schema_version
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'extraction_jobs' AND column_name = 'schema_id'
    ) THEN
        ALTER TABLE extraction_jobs ADD COLUMN schema_id VARCHAR(128);
        ALTER TABLE extraction_jobs ADD COLUMN schema_version VARCHAR(32);
        RAISE NOTICE 'extraction_jobs: added schema_id + schema_version columns';
    ELSE
        RAISE NOTICE 'extraction_jobs: schema_id column already present (skipped)';
    END IF;
END $$;

-- 5-7. Indexes
CREATE INDEX IF NOT EXISTS ix_schema_active
    ON extraction_schemas(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS ix_schema_slug
    ON extraction_schemas(schema_id);
CREATE INDEX IF NOT EXISTS ix_schema_json_gin
    ON extraction_schemas USING GIN (schema_json);

CREATE INDEX IF NOT EXISTS ix_ej_schema
    ON extraction_jobs(schema_id) WHERE schema_id IS NOT NULL;

-- HNSW index on clause_embeddings (if the table exists from v1; if not,
-- schema.sql will create both table and index for fresh installs)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'clause_embeddings') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'ix_clause_emb_hnsw') THEN
            EXECUTE 'CREATE INDEX ix_clause_emb_hnsw ON clause_embeddings
                     USING hnsw (embedding vector_cosine_ops)
                     WITH (m = 16, ef_construction = 64)';
            RAISE NOTICE 'created HNSW index on clause_embeddings.embedding';
        END IF;
    ELSE
        RAISE NOTICE 'clause_embeddings table not present yet — run schema.sql for fresh setup';
    END IF;
END $$;

-- 8. schema_version sentinel
CREATE TABLE IF NOT EXISTS schema_version (
    version     VARCHAR(32)     PRIMARY KEY,
    applied_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    applied_by  VARCHAR(255),
    notes       TEXT
);

INSERT INTO schema_version (version, applied_by, notes)
VALUES ('2.0.0', CURRENT_USER, 'Upgrade migration: extraction_schemas + audit_log nullable + indexes')
ON CONFLICT (version) DO UPDATE
    SET applied_at = NOW(), applied_by = EXCLUDED.applied_by, notes = EXCLUDED.notes;

COMMIT;

\echo
\echo '====================================================='
\echo '  v2.0.0 upgrade migration complete.'
\echo '====================================================='
\echo

SELECT version, applied_at, applied_by, notes
FROM schema_version
ORDER BY applied_at DESC
LIMIT 5;
