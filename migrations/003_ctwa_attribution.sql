-- 003_ctwa_attribution.sql — atribución de campañas Click-to-WhatsApp (Meta Ads).
-- Idempotente: se re-ejecuta completo en cada arranque (Constitución III).

ALTER TABLE bot_conversation ADD COLUMN IF NOT EXISTS ctwa_clid TEXT;
