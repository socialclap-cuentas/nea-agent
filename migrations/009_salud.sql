-- 070 — Vertical salud: la oferta recuerda con QUIÉN y para QUÉ es cada horario.
-- Idempotente (se aplica en cada arranque). Sin profesionales quedan en NULL y
-- todo se comporta como antes.
ALTER TABLE offered_slots ADD COLUMN IF NOT EXISTS professional_id TEXT;
ALTER TABLE offered_slots ADD COLUMN IF NOT EXISTS professional_name TEXT;
ALTER TABLE offered_slots ADD COLUMN IF NOT EXISTS appointment_type_id TEXT;
