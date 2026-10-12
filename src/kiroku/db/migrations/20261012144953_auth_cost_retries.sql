-- Standalone costs must survive a lost commit receipt without consuming a Window.
ALTER TABLE cost_ledger ADD COLUMN idempotency_key uuid UNIQUE;
