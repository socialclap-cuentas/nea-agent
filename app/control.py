"""Pausa global del agente (kill switch).

Dos capas: `AGENT_PAUSED=true` (entorno, freno duro de despliegue) y el flag
`paused` en la base, que se maneja desde el panel /admin. Pausado = Nea NO
responde, no manda seguimientos y no reintenta envíos pendientes. Los mensajes
siguen llegando al CRM (relay intacto) para que una persona los atienda.
Fail-open a propósito: si la base no responde se asume NO pausado — el
webhook y los turnos ya degradan solos; pausar por error callaría al negocio.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("nea.control")

PAUSE_KEY = "paused"
CACHE_SECONDS = 3.0


async def is_paused(ctx: Any) -> bool:
    if getattr(ctx.settings, "agent_paused", False):
        return True
    cache = ctx.pause_cache
    now = time.monotonic()
    if cache and now - cache.get("at", 0.0) < CACHE_SECONDS:
        return bool(cache["value"])
    try:
        value = (await ctx.store.get_flag(PAUSE_KEY)) == "1"
    except Exception:
        logger.exception("control: no pude leer el flag de pausa — asumo activo")
        return False
    cache.update(at=now, value=value)
    return value


async def set_paused(ctx: Any, paused: bool) -> None:
    await ctx.store.set_flag(PAUSE_KEY, "1" if paused else "0")
    ctx.pause_cache.clear()
    logger.warning("control: agente %s desde el panel", "PAUSADO" if paused else "REACTIVADO")
