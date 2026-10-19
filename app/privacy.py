"""Webhooks de privacidad que Tiendanube exige a toda app (store/redact,
customers/redact, customers/data_request).

Nea NO guarda datos de clientes de Tiendanube: solo lee pedidos/productos en vivo
y no los persiste (la conversación de WhatsApp vive en el CRM). Por eso no hay
nada que borrar ni exportar: se acusa recibo con 200.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger("nea.privacy")
router = APIRouter(prefix="/tiendanube")


@router.post("/store-redact")
async def store_redact() -> dict[str, bool]:
    logger.info("tiendanube: store/redact recibido (sin datos propios que borrar)")
    return {"ok": True}


@router.post("/customers-redact")
async def customers_redact() -> dict[str, bool]:
    logger.info("tiendanube: customers/redact recibido (sin datos propios que borrar)")
    return {"ok": True}


@router.post("/customers-data-request")
async def customers_data_request() -> dict[str, bool]:
    logger.info("tiendanube: customers/data_request recibido (sin datos propios)")
    return {"ok": True}
