-- newsrelay schema v1. All timestamps: ISO-8601 UTC text ('YYYY-MM-DDTHH:MM:SSZ').

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE runs (
    id               TEXT PRIMARY KEY,
    run_key          TEXT NOT NULL UNIQUE,
    scheduled_for    TEXT,
    research_from    TEXT NOT NULL,
    research_through TEXT,
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    status           TEXT NOT NULL CHECK (status IN ('started','completed_noop','published','abandoned')),
    candidate_count  INTEGER NOT NULL DEFAULT 0,
    accepted_count   INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX runs_status ON runs(status, started_at);

-- Single-row research checkpoint. Only advanced by successful completion.
CREATE TABLE checkpoint (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    research_through TEXT NOT NULL,
    run_id           TEXT REFERENCES runs(id),
    updated_at       TEXT NOT NULL
) STRICT;

-- Permanent, compact topic identity (never pruned).
CREATE TABLE topics (
    id                   TEXT PRIMARY KEY,
    topic_key            TEXT NOT NULL UNIQUE,
    title                TEXT NOT NULL,
    norm_title           TEXT NOT NULL,
    state_summary        TEXT NOT NULL DEFAULT '',
    category             TEXT NOT NULL,
    entities             TEXT NOT NULL DEFAULT '[]',
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    last_material_update TEXT,
    last_posted          TEXT,
    state                TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','dormant','archived')),
    importance           INTEGER NOT NULL DEFAULT 0,
    story_count          INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX topics_state ON topics(state, last_seen);

CREATE TABLE topic_aliases (
    topic_id   TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    alias_norm TEXT NOT NULL,
    PRIMARY KEY (topic_id, alias_norm)
) STRICT, WITHOUT ROWID;
CREATE INDEX topic_aliases_alias ON topic_aliases(alias_norm);

-- Full-text index over compact topic text (title, aliases, entities, facts).
CREATE VIRTUAL TABLE topic_fts USING fts5(
    topic_id UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE stories (
    id                TEXT PRIMARY KEY,
    topic_id          TEXT NOT NULL REFERENCES topics(id),
    run_id            TEXT REFERENCES runs(id),
    candidate_id      TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('NEW','UPDATE','CORRECTION')),
    headline          TEXT NOT NULL,
    norm_title        TEXT NOT NULL,
    key_facts         TEXT NOT NULL,          -- JSON list of short facts (kept permanently, small)
    material_change   TEXT,
    confidence        TEXT NOT NULL,
    category          TEXT NOT NULL,
    event_time        TEXT,
    content_fp        TEXT NOT NULL,
    title_fp          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    posted_at         TEXT,
    publication_state TEXT NOT NULL DEFAULT 'queued'
                      CHECK (publication_state IN ('queued','delivered','partial','uncertain','failed')),
    body              TEXT,                   -- prunable bulky text
    UNIQUE (run_id, candidate_id)
) STRICT;
CREATE INDEX stories_topic ON stories(topic_id, created_at);
CREATE INDEX stories_content_fp ON stories(content_fp);
CREATE INDEX stories_title_fp ON stories(title_fp);

CREATE TABLE story_sources (
    story_id      TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    url_hash      TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    domain        TEXT NOT NULL,
    source_name   TEXT,
    PRIMARY KEY (story_id, url_hash)
) STRICT, WITHOUT ROWID;
CREATE INDEX story_sources_hash ON story_sources(url_hash);

CREATE TABLE publication_batches (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL UNIQUE REFERENCES runs(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    story_count     INTEGER NOT NULL
) STRICT;

CREATE TABLE outbox (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id           TEXT NOT NULL REFERENCES publication_batches(id),
    story_id           TEXT REFERENCES stories(id),
    seq                INTEGER NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    payload            TEXT,                  -- prunable after delivery
    state              TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending','dispatching','delivered','uncertain','failed','cancelled')),
    attempts           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    next_attempt_at    TEXT NOT NULL,
    last_error         TEXT,
    discord_message_id TEXT,
    delivered_at       TEXT
) STRICT;
CREATE INDEX outbox_due ON outbox(state, next_attempt_at, id);
CREATE INDEX outbox_batch ON outbox(batch_id, seq);

CREATE TABLE delivery_attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id        INTEGER NOT NULL REFERENCES outbox(id) ON DELETE CASCADE,
    attempt_no       INTEGER NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    http_status      INTEGER,
    error            TEXT
) STRICT;
CREATE INDEX delivery_attempts_outbox ON delivery_attempts(outbox_id);
CREATE INDEX delivery_attempts_started ON delivery_attempts(started_at);

-- OAuth 2.1 authorization server state (tokens/codes stored only as SHA-256 hashes).
CREATE TABLE oauth_clients (
    client_id    TEXT PRIMARY KEY,
    client_info  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
) STRICT;

CREATE TABLE oauth_pending (
    id         TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    params     TEXT NOT NULL,
    expires_at REAL NOT NULL
) STRICT;

CREATE TABLE oauth_codes (
    code_hash  TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
) STRICT;

CREATE TABLE oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access','refresh')),
    client_id  TEXT NOT NULL,
    scopes     TEXT NOT NULL,
    resource   TEXT,
    grant_id   TEXT NOT NULL,
    expires_at INTEGER,
    created_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX oauth_tokens_grant ON oauth_tokens(grant_id);
CREATE INDEX oauth_tokens_expiry ON oauth_tokens(expires_at);

CREATE TABLE auth_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL
) STRICT;
