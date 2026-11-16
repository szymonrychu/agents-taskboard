-- Approvals: a typed kind of decision (S-63). An approval is a decision with `kind = 'approval'`,
-- a `subject` (what is approved), a mandatory `expires_at` and the channel that answered it
-- (`answered_via`, `answer_ref`). A lapsed approval ends `expired`.
--
-- Notices: a question notice folds per (task, session, reason), but two approvals on one task
-- must each reach the session, so an approval notice carries its `decision_id` and is unique per
-- decision instead (notices_one_pending_approval_idx); the old index skips approvals.
--
-- decisions_open_approval_expiry_idx serves the expiry sweep of the notifier. Every statement is
-- idempotent; the new CHECKs hold for existing rows (no approval rows exist yet).
--
-- lock_timeout: fail fast rather than queue behind a notifier row lock held across an HTTP send
-- (LOCAL: the migrator runs this file in its own transaction).

SET LOCAL lock_timeout = '5s';

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'question';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS subject text;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS answered_via text;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS answer_ref text;

ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_kind_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_kind_check
    CHECK (kind IN ('question', 'approval'));
ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_status_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_status_check
    CHECK (status IN ('open', 'answered', 'withdrawn', 'expired'));
ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_approval_expires;
ALTER TABLE decisions ADD CONSTRAINT decisions_approval_expires
    CHECK (kind <> 'approval' OR expires_at IS NOT NULL);

CREATE INDEX IF NOT EXISTS decisions_open_approval_expiry_idx
    ON decisions (expires_at) WHERE kind = 'approval' AND status = 'open';

ALTER TABLE notices ADD COLUMN IF NOT EXISTS decision_id bigint
    REFERENCES decisions (id) ON DELETE CASCADE;
ALTER TABLE notices DROP CONSTRAINT IF EXISTS notices_reason_check;
ALTER TABLE notices ADD CONSTRAINT notices_reason_check
    CHECK (reason IN ('decision', 'label', 'spec', 'linked', 'approval'));
ALTER TABLE notices DROP CONSTRAINT IF EXISTS notices_approval_decision;
ALTER TABLE notices ADD CONSTRAINT notices_approval_decision
    CHECK ((reason = 'approval') = (decision_id IS NOT NULL));

DROP INDEX IF EXISTS notices_one_pending_idx;
CREATE UNIQUE INDEX IF NOT EXISTS notices_one_pending_idx
    ON notices (task_id, session_id, reason) WHERE status = 'pending' AND reason <> 'approval';
CREATE UNIQUE INDEX IF NOT EXISTS notices_one_pending_approval_idx
    ON notices (decision_id) WHERE reason = 'approval' AND status = 'pending';
