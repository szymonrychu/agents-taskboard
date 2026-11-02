-- agents-taskboard schema. State columns are text + CHECK (no enums). The task key
-- ("T-<id>") is derived in code and never stored. SQL stays PG16 compatible.

CREATE TABLE tasks (
    id                  bigserial PRIMARY KEY,
    title               text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    body                text NOT NULL DEFAULT '',
    kind                text NOT NULL CHECK (kind IN ('idea', 'task')),
    status              text NOT NULL CHECK (status IN ('idea', 'open', 'in_progress', 'done', 'dropped')),
    labels              text[] NOT NULL DEFAULT '{}',
    assignee_session    text,
    created_by_kind     text NOT NULL CHECK (created_by_kind IN ('user', 'agent')),
    created_by_session  text,
    created_by_slug     text,
    client_key          text,
    version             integer NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_labels_gin_idx ON tasks USING gin (labels);
CREATE UNIQUE INDEX tasks_creator_client_key_idx
    ON tasks (created_by_session, client_key) WHERE client_key IS NOT NULL;

CREATE TABLE comments (
    id              bigserial PRIMARY KEY,
    task_id         bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    author_kind     text NOT NULL CHECK (author_kind IN ('user', 'agent')),
    author_session  text,
    author_slug     text,
    body            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX comments_task_idx ON comments (task_id, id);

CREATE TABLE decisions (
    id              bigserial PRIMARY KEY,
    task_id         bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    asking_session  text NOT NULL,
    asking_slug     text,
    question        text NOT NULL,
    context         text NOT NULL DEFAULT '',
    options         jsonb,
    client_key      text,
    status          text NOT NULL CHECK (status IN ('open', 'answered', 'withdrawn')),
    answer          jsonb,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    answered_at     timestamptz,
    CONSTRAINT decisions_answered_has_answer CHECK (status <> 'answered' OR answer IS NOT NULL)
);

CREATE INDEX decisions_open_idx ON decisions (task_id) WHERE status = 'open';
CREATE UNIQUE INDEX decisions_client_key_idx
    ON decisions (task_id, asking_session, client_key) WHERE client_key IS NOT NULL;

CREATE TABLE todo_items (
    id          bigserial PRIMARY KEY,
    task_id     bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    text        text NOT NULL,
    status      text NOT NULL CHECK (status IN ('todo', 'inprogress', 'done')),
    position    integer NOT NULL
);

CREATE INDEX todo_items_task_idx ON todo_items (task_id, position);

CREATE TABLE events (
    id             bigserial PRIMARY KEY,
    task_id        bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    actor_kind     text NOT NULL CHECK (actor_kind IN ('user', 'agent')),
    actor_session  text,
    actor_slug     text,
    kind           text NOT NULL,
    detail         jsonb,
    at             timestamptz NOT NULL DEFAULT now(),
    scanned_at     timestamptz
);

CREATE INDEX events_task_idx ON events (task_id, id);
CREATE INDEX events_unscanned_label_added_idx
    ON events (id) WHERE kind = 'label_added' AND scanned_at IS NULL;

CREATE TABLE notices (
    id          bigserial PRIMARY KEY,
    task_id     bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    session_id  text NOT NULL,
    reason      text NOT NULL CHECK (reason IN ('decision', 'label')),
    status      text NOT NULL CHECK (status IN ('pending', 'sent')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    attempts    integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX notices_one_pending_idx
    ON notices (task_id, session_id, reason) WHERE status = 'pending';
CREATE UNIQUE INDEX notices_one_label_idx
    ON notices (task_id, session_id) WHERE reason = 'label';
