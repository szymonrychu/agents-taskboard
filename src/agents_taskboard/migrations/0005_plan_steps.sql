-- Plan header and plan steps (task-plans stage 4, base design 3.17): a plan is a header on the
-- spec plus an ordered set of steps, and a step is a task of the spec (`tasks.step`, dense from
-- 1). `specs.steps_version` is bumped by every change of the step set or order, so a reorder
-- from the web and a bulk set from an agent cannot interleave. Every statement is idempotent.

CREATE TABLE IF NOT EXISTS spec_plans (
    spec_id             bigint PRIMARY KEY REFERENCES specs (id) ON DELETE CASCADE,
    goal                text NOT NULL DEFAULT '' CHECK (length(goal) <= 500),
    architecture        text NOT NULL DEFAULT '' CHECK (length(architecture) <= 4000),
    tech_stack          text NOT NULL DEFAULT '' CHECK (length(tech_stack) <= 1000),
    constraints         jsonb NOT NULL DEFAULT '[]'
                        CHECK (jsonb_typeof(constraints) = 'array'
                               AND jsonb_array_length(constraints) <= 50),
    review_focus        jsonb NOT NULL DEFAULT '[]'
                        CHECK (jsonb_typeof(review_focus) = 'array'
                               AND jsonb_array_length(review_focus) <= 50),
    version             integer NOT NULL DEFAULT 1,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by_kind     text NOT NULL CHECK (updated_by_kind IN ('user', 'agent')),
    updated_by_session  text,
    updated_by_slug     text
);

ALTER TABLE specs ADD COLUMN IF NOT EXISTS steps_version integer NOT NULL DEFAULT 0;

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS step integer;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS refs jsonb NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS model text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_step_needs_spec') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_step_needs_spec
            CHECK (step IS NULL OR (spec_id IS NOT NULL AND step >= 1));
    END IF;
    -- Deferred, so renumbering the steps of a spec in one transaction works.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_spec_step_key') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_spec_step_key UNIQUE (spec_id, step)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_refs_array') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_refs_array
            CHECK (jsonb_typeof(refs) = 'array' AND jsonb_array_length(refs) <= 20);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tasks_model_check') THEN
        ALTER TABLE tasks ADD CONSTRAINT tasks_model_check
            CHECK (model IS NULL OR model IN ('haiku', 'sonnet', 'opus', 'fable'));
    END IF;
END
$$;

-- Dependencies between steps of one spec; cycles are refused at write (db/steps.py).
CREATE TABLE IF NOT EXISTS task_deps (
    task_id     bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    depends_on  bigint NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on),
    CONSTRAINT task_deps_not_self CHECK (task_id <> depends_on)
);

CREATE INDEX IF NOT EXISTS task_deps_depends_on_idx ON task_deps (depends_on);
