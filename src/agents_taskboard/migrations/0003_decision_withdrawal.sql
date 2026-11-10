-- Withdrawal outcome (T-102): a withdrawn decision records why and what replaced it, either a
-- superseding decision or a free-text note that the question was answered elsewhere. The CHECK is
-- NOT VALID on purpose: withdrawn rows from before this migration have no outcome and are never
-- updated again (writes are guarded on status = 'open'), so they stay as they are while every
-- new or changed row is checked.

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS withdraw_reason text;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS superseded_by bigint REFERENCES decisions (id);
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS answered_elsewhere text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'decisions_withdrawn_has_outcome'
    ) THEN
        ALTER TABLE decisions ADD CONSTRAINT decisions_withdrawn_has_outcome CHECK (
            status <> 'withdrawn'
            OR (withdraw_reason IS NOT NULL
                AND (superseded_by IS NULL) <> (answered_elsewhere IS NULL))
        ) NOT VALID;
    END IF;
END
$$;
