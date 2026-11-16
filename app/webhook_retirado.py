"""El webhook de Meta que Nea ya no recibe (2.0).

Desde la 2.0 Meta le manda el webhook al CRM, y el CRM le despacha a Nea la
ráfaga (app/dispatch.py). Pero quien actualiza Nea antes de devolverle el
webhook al CRM sigue recibiendo aquí los mensajes de sus clientes, y un 404
los perdería sin que nadie se enterara.

Por eso esta ruta contesta **503**: Meta reintenta en vez de dar el mensaje por
entregado, y en cuanto el webhook apunta al CRM los mensajes siguen llegando.
Mientras tanto, un error en el log —como mucho uno por minuto— dice qué hacer,
y `/health` enseña cuándo llegó el último (`webhookDeMeta.ultimoEn`).

La verificación (`GET`) responde 404: suscribir a Nea a Meta ya no tiene
sentido, y un challenge contestado lo haría parecer correcto.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.state import AppContext, utcnow

logger = logging.getLogger("nea.webhook_retirado")

router = APIRouter()

AVISO_CADA = timedelta(minutes=1)

AVISO = (
    "Meta le está mandando el webhook a Nea, y desde la 2.0 Nea solo recibe el "
    "despacho del CRM. Contesté 503 para que Meta lo reintente: devuélvele el "
    "webhook al CRM (override de la WABA o del número hacia "
    "/api/webhooks/wa/<token> del CRM) y pon BRAIN_DISPATCH_URL en el CRM "
    "apuntando a /vocero/dispatch de Nea."
)


# Cabeceras que el CRM necesita para validar y leer el cuerpo tal cual lo firmó
# Meta. Nada más viaja: ni cookies ni cabeceras del proxy.
_CABECERAS_DE_META = ("content-type", "x-hub-signature-256", "x-hub-signature")

PUENTE_TIMEOUT_S = 10.0


def _destino_del_puente() -> str:
    """URL del webhook del CRM (la `CRM_WEBHOOK_URL` de Nea 1.x), si está puesta.

    Con ella, una instalación cuyo webhook de Meta todavía apunta a Nea sigue
    funcionando: Nea reenvía el mensaje tal cual al CRM, y el CRM se lo despacha
    de vuelta por `/vocero/dispatch`. Sin ella, el comportamiento es el de
    siempre: 503 y aviso.
    """
    return os.environ.get("CRM_WEBHOOK_URL", "").strip()


async def _reenviar(request: Request, ctx: AppContext, destino: str) -> Response:
    ctx.webhook_meta_ultimo = utcnow()
    cuerpo = await request.body()
    cabeceras = {
        k: v for k, v in request.headers.items() if k.lower() in _CABECERAS_DE_META
    }
    try:
        async with httpx.AsyncClient(timeout=PUENTE_TIMEOUT_S) as http:
            respuesta = await http.post(destino, content=cuerpo, headers=cabeceras)
    except httpx.HTTPError as exc:
        # Meta reintenta ante un 503: el mensaje no se da por entregado.
        logger.error("el puente al CRM falló (%s); contesté 503 para que Meta reintente", type(exc).__name__)
        return JSONResponse({"error": "crm_no_disponible"}, status_code=503)
    return Response(
        content=respuesta.content,
        status_code=respuesta.status_code,
        media_type=respuesta.headers.get("content-type"),
    )


async def _entrada(request: Request) -> Response:
    ctx = request.app.state.ctx
    destino = _destino_del_puente()
    if destino:
        return await _reenviar(request, ctx, destino)
    return _registrar(ctx)


def _registrar(ctx: AppContext) -> JSONResponse:
    ahora = utcnow()
    anterior = ctx.webhook_meta_ultimo
    ctx.webhook_meta_ultimo = ahora
    if anterior is None or ahora - anterior >= AVISO_CADA:
        logger.error(AVISO)
    return JSONResponse(
        {"error": "webhook_retirado", "detalle": "Nea 2.0 recibe el despacho del CRM"},
        status_code=503,
    )


@router.post("/webhook")
async def webhook(request: Request) -> Response:
    return await _entrada(request)


@router.post("/webhook/{segmento}")
async def webhook_con_segmento(segmento: str, request: Request) -> Response:
    return await _entrada(request)


@router.get("/webhook")
async def verificacion() -> JSONResponse:
    return JSONResponse({"error": "no_encontrado"}, status_code=404)


@router.get("/webhook/{segmento}")
async def verificacion_con_segmento(segmento: str) -> JSONResponse:
    return JSONResponse({"error": "no_encontrado"}, status_code=404)
