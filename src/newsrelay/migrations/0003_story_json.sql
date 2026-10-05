-- Full validated story (impact, outlook, region, importance, sources) for the briefing PDF and the
-- RSVP reader. NULL for stories published before this migration.
ALTER TABLE stories ADD COLUMN story_json TEXT;
