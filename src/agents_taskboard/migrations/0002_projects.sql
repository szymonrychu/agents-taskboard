-- Projects (projects plan 3.6): every task belongs to one project; existing tasks go to
-- `default`. Events carry the project of their task at write time (the stream and catch-up
-- filter on it); the column stays nullable for rows that have no task.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS tasks_project_status_idx ON tasks (project, status);

ALTER TABLE events ADD COLUMN IF NOT EXISTS project text;
UPDATE events e SET project = t.project FROM tasks t WHERE t.id = e.task_id AND e.project IS NULL;
