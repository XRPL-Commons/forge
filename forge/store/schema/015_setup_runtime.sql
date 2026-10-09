ALTER TABLE setup_instances ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);
