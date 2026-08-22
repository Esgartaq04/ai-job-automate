-- Outcome tracking needs to be idempotent: the Gmail query is time-windowed, so
-- the same message is fetched on consecutive runs. Without this, a second pass
-- over an interview email would try to re-transition an application that has
-- already moved on.

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id      TEXT PRIMARY KEY,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    category        TEXT NOT NULL,
    application_id  UUID REFERENCES applications(id) ON DELETE SET NULL,
    detail          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS processed_messages_application_idx
    ON processed_messages(application_id);
