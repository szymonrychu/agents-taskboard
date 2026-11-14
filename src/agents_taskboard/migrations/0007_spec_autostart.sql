-- Spec approval auto-start (S-51): the start queue and the per-project autostart limits.
-- No defaults: an absent project_settings row or a NULL value means autostart is off.
ALTER TABLE specs ADD COLUMN IF NOT EXISTS preset_id text;

CREATE TABLE IF NOT EXISTS spec_starts (
  id bigserial PRIMARY KEY,
  spec_id bigint NOT NULL REFERENCES specs (id) ON DELETE CASCADE,
  approval_event_id bigint NOT NULL,
  ordinal int NOT NULL,
  project text NOT NULL,
  status text NOT NULL DEFAULT 'new'
    CHECK (status IN ('new','queued','in_progress','handled','failed','removed')),
  position int,
  mode text CHECK (mode IN ('nudged','started')),
  owner_session text, slug text, preset_id text, session_id text, message_id text,
  error text, hold_reason text,
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (spec_id, approval_event_id),
  UNIQUE (spec_id, ordinal),
  CHECK ((status = 'queued') = (position IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS spec_starts_queue_idx ON spec_starts (project, position) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS spec_starts_open_idx ON spec_starts (next_attempt_at, id) WHERE status IN ('new','in_progress');

CREATE TABLE IF NOT EXISTS project_settings (
  project text PRIMARY KEY,
  max_active_sessions int CHECK (max_active_sessions >= 1),
  max_usage_pct int CHECK (max_usage_pct BETWEEN 1 AND 100),
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
