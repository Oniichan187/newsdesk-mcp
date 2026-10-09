-- Stories handed over in small batches before the run is published (newsrelay_stage_stories).
-- newsrelay_publish_digest (or the worker, if that call never arrives) publishes them together.
CREATE TABLE staged_stories (
    run_key          TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    research_through TEXT NOT NULL,
    story_json       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (run_key, candidate_id)
) STRICT;
CREATE INDEX staged_stories_created ON staged_stories(created_at);
