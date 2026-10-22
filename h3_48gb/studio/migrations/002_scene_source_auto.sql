-- The frame source of a scene is either named by the owner or derived from the order
-- («the previous scene»). A derived one is recomputed when the order changes or the scene it
-- points at is deleted; a named one is not. Existing rows count as named (nothing derived them).
ALTER TABLE scene ADD COLUMN source_auto INTEGER NOT NULL DEFAULT 0 CHECK (source_auto IN (0, 1));
