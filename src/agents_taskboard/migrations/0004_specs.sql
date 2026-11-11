-- Specs (task-plans stage 3a, base design 3.5): a spec is a named bundle of markdown documents
-- of one project; every document has a linear, append-only version history. The key ("S-<id>")
-- is derived in code. A user delete is a soft delete (`deleted_at`): the name is freed and the
-- spec's tasks are detached, its event rows stay for stream catch-up. No `created_by_device`
-- columns: the device path is stage 3b. Every statement is idempotent.

CREATE TABLE IF NOT EXISTS specs (
    id                  bigserial PRIMARY KEY,
    project             text NOT NULL DEFAULT 'default',
    name                text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,79}$'),
    title               text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'review', 'approved', 'archived')),
    labels              text[] NOT NULL DEFAULT '{}',
    assignee_session    text,
    assignee_slug       text,
    created_by_kind     text NOT NULL CHECK (created_by_kind IN ('user', 'agent')),
    created_by_session  text,
    created_by_slug     text,
    client_key          text,
    version             integer NOT NULL DEFAULT 1,
    deleted_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT specs_id_project_key UNIQUE (id, project)
);

CREATE UNIQUE INDEX IF NOT EXISTS specs_project_name_idx
    ON specs (project, name) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS specs_creator_client_key_idx
    ON specs (created_by_session, client_key) WHERE client_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS specs_project_status_idx ON specs (project, status);

CREATE TABLE IF NOT EXISTS spec_versions (
    spec_id             bigint NOT NULL REFERENCES specs (id) ON DELETE CASCADE,
    path                text NOT NULL
                        CHECK (path ~ '^[a-z0-9][a-z0-9._/-]{0,119}\.md$' AND path !~ '\.\.'),
    version             integer NOT NULL CHECK (version >= 1),
    body                text,
    sha256              text,
    size                integer,
    note                text CHECK (length(note) <= 500),
    sources             jsonb NOT NULL DEFAULT '[]',
    created_by_kind     text NOT NULL CHECK (created_by_kind IN ('user', 'agent')),
    created_by_session  text,
    created_by_slug     text,
    client_key          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (spec_id, path, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS spec_versions_client_key_idx
    ON spec_versions (spec_id, client_key) WHERE client_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS spec_comments (
    id                  bigserial PRIMARY KEY,
    spec_id             bigint NOT NULL REFERENCES specs (id) ON DELETE CASCADE,
    path                text,
    version             integer,
    anchor              text,
    body                text NOT NULL,
    suggestion          text,
    suggestion_status   text CHECK (suggestion_status IN ('open', 'accepted', 'dismissed')),
    accepted_version    integer,
    decided_at          timestamptz,
    decided_by_kind     text CHECK (decided_by_kind IN ('user', 'agent')),
    decided_by_session  text,
    decided_by_slug     text,
    attachments         jsonb NOT NULL DEFAULT '[]',
    resolved_at         timestamptz,
    resolved_by_kind    text CHECK (resolved_by_kind IN ('user', 'agent')),
    resolved_by_session text,
    resolved_by_slug    text,
    created_by_kind     text NOT NULL CHECK (created_by_kind IN ('user', 'agent')),
    created_by_session  text,
    created_by_slug     text,
    client_key          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT spec_comments_suggestion_has_status
        CHECK ((suggestion IS NULL) = (suggestion_status IS NULL))
);

CREATE INDEX IF NOT EXISTS spec_comments_spec_idx ON spec_comments (spec_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS spec_comments_client_key_idx
    ON spec_comments (spec_id, created_by_session, client_key) WHERE client_key IS NOT NULL;

-- A task in a spec is always in the spec's project: moving a spec moves its tasks in the same
-- statement (ON UPDATE CASCADE), a hard delete only detaches them.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS spec_id bigint;
CREATE INDEX IF NOT EXISTS tasks_spec_idx ON tasks (spec_id) WHERE spec_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_spec_fk') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_spec_fk FOREIGN KEY (spec_id, project)
            REFERENCES specs (id, project) ON UPDATE CASCADE ON DELETE SET NULL (spec_id);
    END IF;
END
$$;

-- Events of a spec have no task: exactly one of task_id and spec_id is set.
ALTER TABLE events ADD COLUMN IF NOT EXISTS spec_id bigint REFERENCES specs (id) ON DELETE CASCADE;
ALTER TABLE events ALTER COLUMN task_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS events_spec_idx ON events (spec_id, id) WHERE spec_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'events_one_subject') THEN
        ALTER TABLE events ADD CONSTRAINT events_one_subject
            CHECK (num_nonnulls(task_id, spec_id) = 1);
    END IF;
END
$$;

-- Notices about a spec (reason 'spec') have no task.
ALTER TABLE notices ADD COLUMN IF NOT EXISTS spec_id bigint REFERENCES specs (id) ON DELETE CASCADE;
ALTER TABLE notices ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE notices DROP CONSTRAINT IF EXISTS notices_reason_check;
ALTER TABLE notices ADD CONSTRAINT notices_reason_check
    CHECK (reason IN ('decision', 'label', 'spec'));

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'notices_one_subject') THEN
        ALTER TABLE notices ADD CONSTRAINT notices_one_subject
            CHECK (num_nonnulls(task_id, spec_id) = 1);
    END IF;
END
$$;
