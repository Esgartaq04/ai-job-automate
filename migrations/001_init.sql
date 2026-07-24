-- AutoApply Phase 1 schema.
-- Mirrors README section 5. Applied by `autoapply init-db` (idempotent).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS profiles (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL DEFAULT 1,
    base_resume_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding         vector(1536),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, version)
);

-- The fact store. Every generated resume bullet must cite a row here.
CREATE TABLE IF NOT EXISTS facts (
    id           TEXT PRIMARY KEY,
    profile_id   UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,            -- experience | education | project | skill
    org          TEXT,
    role         TEXT,
    start_date   TEXT,                     -- 'YYYY-MM'; text because precision varies
    end_date     TEXT,
    text         TEXT NOT NULL,            -- one verified claim, verbatim
    skills       TEXT[] NOT NULL DEFAULT '{}',
    embedding    vector(1536),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_profile_idx ON facts(profile_id);

CREATE TABLE IF NOT EXISTS companies (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    ats_type     TEXT NOT NULL,            -- greenhouse | lever | ashby | workable
    board_token  TEXT NOT NULL,
    careers_url  TEXT,
    active       BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ats_type, board_token)
);

CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ats_job_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    description     TEXT NOT NULL DEFAULT '',
    apply_url       TEXT NOT NULL,
    remote          BOOLEAN NOT NULL DEFAULT false,
    canonical_hash  TEXT NOT NULL UNIQUE,  -- dedup across sources
    content_hash    TEXT NOT NULL,         -- embedding cache key
    posted_at       TIMESTAMPTZ,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_company_idx ON jobs(company_id);

CREATE TABLE IF NOT EXISTS matches (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id     UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    score          DOUBLE PRECISION NOT NULL,
    similarity     DOUBLE PRECISION,
    reasons_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    blockers_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, profile_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT NOT NULL,             -- resume_pdf | cover_letter | screenshot | dom_snapshot
    uri         TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS artifacts_sha_idx ON artifacts(sha256);

CREATE TABLE IF NOT EXISTS applications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id              UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    state               TEXT NOT NULL DEFAULT 'discovered',
    resume_artifact_id  UUID REFERENCES artifacts(id),
    cover_artifact_id   UUID REFERENCES artifacts(id),
    tailored_json       JSONB,             -- bullets + fact_id citations, for the review diff
    needs_input_json    JSONB,             -- open questions when state = needs_input
    submitted_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The single line that prevents the embarrassing double-apply.
    UNIQUE (user_id, job_id)
);
CREATE INDEX IF NOT EXISTS applications_state_idx ON applications(state);

CREATE TABLE IF NOT EXISTS runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id  UUID REFERENCES applications(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL,         -- ingest | match | tailor | submit | outcome
    trace_id        TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
    cost_cents      DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_application_idx ON runs(application_id);

-- Append-only. This is the source of truth; applications.state is a cache.
CREATE TABLE IF NOT EXISTS run_events (
    id            BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    level         TEXT NOT NULL DEFAULT 'info',
    event_type    TEXT NOT NULL,
    message       TEXT NOT NULL DEFAULT '',
    payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS run_events_run_idx ON run_events(run_id, id);

-- LLM field-mapping answers are cached per (ats, field_signature) so an unknown
-- field costs one model call ever, not one per application. README section 4.
CREATE TABLE IF NOT EXISTS field_map_cache (
    ats_type    TEXT NOT NULL,
    signature   TEXT NOT NULL,
    canonical   TEXT,
    confidence  DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ats_type, signature)
);

CREATE TABLE IF NOT EXISTS credentials (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider          TEXT NOT NULL,
    ciphertext        BYTEA NOT NULL,
    kms_key_version   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider)
);
