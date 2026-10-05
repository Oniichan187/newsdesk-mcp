-- Daily briefing channels (bot mode, optional): one Discord text channel per local day.
-- Before a day channel is deleted, its delivered messages are copied to the archive channel;
-- archived_upto / header_sent make that copy resumable after a crash.
CREATE TABLE discord_day_channels (
    day           TEXT PRIMARY KEY,          -- YYYY-MM-DD in display_timezone
    channel_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','archiving','archived')),
    header_sent   INTEGER NOT NULL DEFAULT 0,
    archived_upto INTEGER NOT NULL DEFAULT 0, -- last outbox.id copied to the archive
    archived_at   TEXT
) STRICT;

-- Channel an outbox item was (or is being) delivered to; NULL = the fixed discord_channel_id.
ALTER TABLE outbox ADD COLUMN channel_id TEXT;
CREATE INDEX outbox_channel ON outbox(channel_id, state, id);
