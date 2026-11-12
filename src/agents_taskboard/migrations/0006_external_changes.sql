-- External changes (a task linked from another service, e.g. a mindmap node): the `linked`
-- notice reason. events.kind is free text, so `external_change` needs no constraint change; the
-- partial index serves the notifier scan. notices_one_pending_idx (task, session, reason where
-- pending) already makes at most one pending `linked` notice per task and session.
ALTER TABLE notices DROP CONSTRAINT IF EXISTS notices_reason_check;
ALTER TABLE notices ADD CONSTRAINT notices_reason_check
    CHECK (reason IN ('decision', 'label', 'spec', 'linked'));

CREATE INDEX IF NOT EXISTS events_unscanned_external_change_idx
    ON events (id) WHERE kind = 'external_change' AND scanned_at IS NULL;
