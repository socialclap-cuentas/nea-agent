-- 004_bot_flag.sql — interruptores operativos del bot (p. ej. pausa global).
-- Clave/valor simple; idempotente (Constitución III).

CREATE TABLE IF NOT EXISTS bot_flag (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
