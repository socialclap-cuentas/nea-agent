"""Panel de control mínimo: pausar/reactivar el agente y vaciar la cola.

Una sola página (GET /admin) protegida con HTTP Basic: usuario `admin`,
contraseña = ADMIN_TOKEN. Sin ADMIN_TOKEN el panel NO existe (404): jamás se
expone un control sin credencial. Comparación en tiempo constante.
"""
from __future__ import annotations

import html
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.control import is_paused, set_paused

logger = logging.getLogger("nea.admin")

router = APIRouter()
_basic = HTTPBasic(auto_error=False)


def _ctx(request: Request) -> Any:
    ctx = request.app.state.ctx
    if ctx is None:
        raise HTTPException(503, "arrancando")
    return ctx


async def require_admin(
    request: Request, creds: HTTPBasicCredentials | None = Depends(_basic)
) -> Any:
    ctx = _ctx(request)
    token = ctx.settings.admin_token
    if not token:
        raise HTTPException(404)  # panel deshabilitado
    if creds is None or not (
        secrets.compare_digest(creds.username.encode(), b"admin")
        & secrets.compare_digest(creds.password.encode(), token.encode())
    ):
        raise HTTPException(
            401, "credenciales inválidas", headers={"WWW-Authenticate": 'Basic realm="Nea"'}
        )
    return ctx


_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nea — control</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;padding:0 16px;color:#1a1a1a}}
.card{{border:1px solid #ddd;border-radius:12px;padding:20px;margin-bottom:16px}}
.state{{font-size:1.4rem;font-weight:700}}.on{{color:#15803d}}.off{{color:#b91c1c}}
button{{font:inherit;padding:12px 18px;border-radius:8px;border:0;cursor:pointer;width:100%;margin-top:12px}}
.pause{{background:#b91c1c;color:#fff}}.resume{{background:#15803d;color:#fff}}.alt{{background:#eee}}
small{{color:#666}}
</style></head><body>
<h1>Nea · control</h1>
<div class="card">
  <div class="state {cls}">{estado}</div>
  <small>{detalle}</small>
  <form method="post" action="/admin/{accion}"><button class="{btn}">{boton}</button></form>
</div>
<div class="card">
  <b>Respuestas atrasadas en cola: {pendientes}</b><br>
  <small>Se descartan solas a los {max_age} min. Podés vaciarlas ahora.</small>
  <form method="post" action="/admin/vaciar-cola"><button class="alt">Vaciar cola</button></form>
</div>
{aviso}
</body></html>"""


@router.get("/admin", response_class=HTMLResponse)
async def panel(ctx: Any = Depends(require_admin), msg: str = "") -> HTMLResponse:
    env_pause = bool(ctx.settings.agent_paused)
    paused = await is_paused(ctx)
    if env_pause:
        detalle = "Pausado por configuración del servidor (AGENT_PAUSED): no se puede reactivar desde acá."
    elif paused:
        detalle = "El agente NO responde ni manda seguimientos. Los mensajes siguen llegando a la Bandeja del CRM."
    else:
        detalle = "El agente responde con normalidad."
    pendientes = len(await ctx.store.due_pending_sends(_far_future()))
    page = _PAGE.format(
        cls="off" if paused else "on",
        estado="⏸ PAUSADO" if paused else "▶ ACTIVO",
        detalle=html.escape(detalle),
        accion="reactivar" if paused else "pausar",
        btn="resume" if paused else "pause",
        boton="Reactivar agente" if paused else "Pausar agente",
        pendientes=pendientes,
        max_age=ctx.settings.pending_send_max_age_minutes or "∞",
        aviso=f"<p><small>{html.escape(msg)}</small></p>" if msg else "",
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


def _far_future():  # noqa: ANN202 - helper mínimo
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(days=3650)


@router.post("/admin/pausar")
async def pausar(ctx: Any = Depends(require_admin)) -> RedirectResponse:
    await set_paused(ctx, True)
    return RedirectResponse("/admin?msg=Agente+pausado", status_code=303)


@router.post("/admin/reactivar")
async def reactivar(ctx: Any = Depends(require_admin)) -> RedirectResponse:
    await set_paused(ctx, False)
    return RedirectResponse("/admin?msg=Agente+reactivado", status_code=303)


@router.post("/admin/vaciar-cola")
async def vaciar_cola(ctx: Any = Depends(require_admin)) -> RedirectResponse:
    n = await ctx.store.abandon_all_pending_sends()
    logger.warning("admin: cola de envíos vaciada (%d descartados)", n)
    return RedirectResponse(f"/admin?msg={n}+respuestas+descartadas", status_code=303)
