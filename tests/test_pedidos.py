"""Consulta de pedidos y seguimiento (Tiendanube) + webhooks de privacidad."""
from __future__ import annotations

import httpx
import pytest

from app.tiendanube import TiendanubeClient, normalize_order
from app.tools import ToolRuntime
from tests.conftest import CRM_CONV_ID, make_ctx, make_settings

BASE = "https://api.tiendanube.com/v1/777"
WA = "5491132106704"  # identidad de WhatsApp de la clienta

ORDER = {
    "id": 9001,
    "number": 1234,
    "status": "open",
    "payment_status": "paid",
    "shipping_status": "shipped",
    "shipping_option": "Correo Argentino",
    "shipping_tracking_number": "AR123456789",
    "shipping_tracking_url": "https://correo.example/AR123456789",
    "created_at": "2026-10-10T15:30:00+0000",
    "contact_phone": "+54 9 11 3210-6704",
    "contact_email": "Clienta@Mail.com",
    "contact_name": "No Debe Salir",
    "products": [{"name": "DoyPack zipper", "quantity": 2}],
}


@pytest.fixture
async def client():
    c = TiendanubeClient("777", "tok", user_agent="Test (t@t.com)")
    yield c
    await c.aclose()


async def _runtime(tn, identity=WA):
    ctx = make_ctx(make_settings(agent_mode="ventas"))
    ctx.tiendanube = tn
    conv = await ctx.store.get_or_create_conversation(identity)
    return ToolRuntime(ctx, conv, CRM_CONV_ID), ctx


def test_estados_en_espanol_claro():
    out = normalize_order(dict(ORDER, payment_status="voided", shipping_status="unpacked"))
    assert out["estado"] == "abierto"
    assert "anulado" in out["pago"] and "open" not in str(out)
    assert out["envio"] == "todavía sin preparar"
    assert normalize_order(dict(ORDER, shipping_status="algo_nuevo"))["envio"] == "algo_nuevo"


def test_normalize_order_no_expone_datos_personales():
    out = normalize_order(ORDER)
    assert out["numero_seguimiento"] == "AR123456789"
    assert out["productos"] == [{"nombre": "DoyPack zipper", "cantidad": 2}]
    assert "No Debe Salir" not in str(out)


async def test_pedido_verificado_por_celular(client, respx_mock):
    respx_mock.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json=[ORDER]))
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_pedido", {"numero_pedido": 1234})
    assert res["ok"] is True
    p = res["pedido"]
    assert p["numero_seguimiento"] == "AR123456789"
    assert p["link_seguimiento"].startswith("https://")
    assert not any(k.startswith("_") for k in p)  # nada privado al LLM
    assert "Clienta@Mail.com" not in str(res) and "3210" not in str(res)
    await ctx.crm.aclose()


async def test_pedido_ajeno_no_revela_nada(client, respx_mock):
    respx_mock.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json=[ORDER]))
    runtime, ctx = await _runtime(client, identity="5491155550000")
    res = await runtime.execute("buscar_pedido", {"numero_pedido": 1234})
    assert res["ok"] is False and res["error"] == "no_verificado"
    assert "AR123456789" not in str(res)
    await ctx.crm.aclose()


async def test_pedido_verificado_por_email(client, respx_mock):
    respx_mock.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json=[ORDER]))
    runtime, ctx = await _runtime(client, identity="5491155550000")
    bad = await runtime.execute(
        "buscar_pedido", {"numero_pedido": 1234, "email": "otro@mail.com"}
    )
    assert bad["error"] == "no_verificado"
    ok = await runtime.execute(
        "buscar_pedido", {"numero_pedido": 1234, "email": " clienta@mail.com "}
    )
    assert ok["ok"] is True
    await ctx.crm.aclose()


async def test_muestra_otros_pedidos_del_mismo_email(client, respx_mock):
    otro = dict(
        ORDER, number=1500, payment_status="paid", shipping_status="shipped",
        created_at="2026-10-18T10:00:00+0000",
    )
    ajeno = dict(ORDER, number=1600, contact_email="otro@mail.com")
    respx_mock.get(f"{BASE}/orders").mock(
        return_value=httpx.Response(200, json=[ORDER, otro, ajeno])
    )
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_pedido", {"numero_pedido": 1234})
    nums = [o["numero_pedido"] for o in res["otros_pedidos_de_esta_persona"]]
    assert nums == [1500]  # ni el mismo pedido ni el de otra persona
    assert "productos" not in res["otros_pedidos_de_esta_persona"][0]
    assert "Clienta@Mail.com" not in str(res)
    await ctx.crm.aclose()


async def test_pedido_inexistente_y_numero_invalido(client, respx_mock):
    respx_mock.get(f"{BASE}/orders").mock(return_value=httpx.Response(404))
    runtime, ctx = await _runtime(client)
    # mismo error que un pedido ajeno: no se filtra qué números existen
    assert (await runtime.execute("buscar_pedido", {"numero_pedido": 999999}))[
        "error"
    ] == "no_verificado"
    assert (await runtime.execute("buscar_pedido", {"numero_pedido": 7}))[
        "error"
    ] == "numero_pedido_invalido"
    assert (await runtime.execute("buscar_pedido", {"numero_pedido": "abc"}))[
        "error"
    ] == "numero_pedido_invalido"
    await ctx.crm.aclose()


async def test_pedido_sin_seguimiento_avisa(client, respx_mock):
    raw = dict(ORDER, shipping_tracking_number=None, shipping_tracking_url=None)
    respx_mock.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json=[raw]))
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_pedido", {"numero_pedido": 1234})
    assert res["ok"] and res["pedido"]["numero_seguimiento"] is None
    assert "no prometas fechas" in res["pedido"]["nota"]
    await ctx.crm.aclose()


async def test_tiendanube_caido_no_tumba_el_turno(client, respx_mock):
    respx_mock.get(f"{BASE}/orders").mock(return_value=httpx.Response(500))
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_pedido", {"numero_pedido": 1234})
    assert res["ok"] is False and res["error"] == "pedidos_no_disponibles"
    await ctx.crm.aclose()


async def test_webhooks_de_privacidad_responden_200():
    from app.main import create_app

    app = create_app(ctx=make_ctx())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as http:
        for path in ("store-redact", "customers-redact", "customers-data-request"):
            r = await http.post(f"/tiendanube/{path}", json={"store_id": 1})
            assert r.status_code == 200
