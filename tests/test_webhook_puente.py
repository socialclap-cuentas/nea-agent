"""Puente del webhook: con `CRM_WEBHOOK_URL` puesta, Nea reenvía lo de Meta al CRM."""
from __future__ import annotations

import httpx

from tests.conftest import make_ctx
from tests.test_despacho_raiz import _cliente

DESTINO = "http://crm.test/api/webhooks/wa/tok"
CUERPO = b'{"object":"whatsapp_business_account","entry":[]}'


async def test_reenvia_el_cuerpo_y_la_firma_al_crm(monkeypatch, respx_mock):
    monkeypatch.setenv("CRM_WEBHOOK_URL", DESTINO)
    ruta = respx_mock.post(DESTINO).mock(return_value=httpx.Response(200, json={"ok": True}))
    ctx = make_ctx()
    async with _cliente(ctx) as client:
        r = await client.post(
            "/webhook",
            content=CUERPO,
            headers={"content-type": "application/json", "x-hub-signature-256": "sha256=abc", "cookie": "x=1"},
        )
    assert r.status_code == 200
    enviado = ruta.calls[0].request
    assert enviado.content == CUERPO
    assert enviado.headers["x-hub-signature-256"] == "sha256=abc"
    assert "cookie" not in enviado.headers
    await ctx.crm.aclose()


async def test_si_el_crm_no_responde_contesta_503_para_que_meta_reintente(monkeypatch, respx_mock):
    monkeypatch.setenv("CRM_WEBHOOK_URL", DESTINO)
    respx_mock.post(DESTINO).mock(side_effect=httpx.ConnectError("caído"))
    ctx = make_ctx()
    async with _cliente(ctx) as client:
        r = await client.post("/webhook", content=CUERPO)
    assert r.status_code == 503
    await ctx.crm.aclose()


async def test_sin_la_variable_sigue_el_503(monkeypatch):
    monkeypatch.delenv("CRM_WEBHOOK_URL", raising=False)
    ctx = make_ctx()
    async with _cliente(ctx) as client:
        r = await client.post("/webhook", content=CUERPO)
    assert r.status_code == 503
    await ctx.crm.aclose()
