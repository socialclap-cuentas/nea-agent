"""Catálogo de Tiendanube: cliente, herramientas y modo ventas."""
from __future__ import annotations

import httpx
import pytest

from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation
from app.tiendanube import (
    TiendanubeClient,
    TiendanubeError,
    format_ars,
    normalize_product,
)
from app.tools import ToolRuntime, tool_schemas
from tests.conftest import CRM_CONV_ID, IDENTITY, make_ctx, make_settings

BASE = "https://api.tiendanube.com/v1/777"

RAW = {
    "id": 254803098,
    "name": {"es": "DoyPack zipper Papel x 50"},
    "description": {"es": "<p>Cierre <b>zipper</b> &amp; fuelle</p>"},
    "canonical_url": "https://ebags.com.ar/productos/doy-pack-papel/",
    "free_shipping": False,
    "tags": ["DoyPacks"],
    "variants": [
        {
            "id": 1,
            "values": [{"es": "18 x 26"}, {"es": "Papel"}],
            "price": "32161.09",
            "promotional_price": "29000.00",
            "stock": 5,
            "stock_management": True,
            "sku": "DP-1826",
        },
        {"id": 2, "values": [{"es": "20 x 28"}], "price": "1000.00", "stock": 0,
         "stock_management": True},
        {"id": 3, "values": [], "price": "500.00", "stock": None},
    ],
}


def test_format_ars():
    assert format_ars("32161.09") == "$32.161,09"
    assert format_ars(1000000) == "$1.000.000,00"
    assert format_ars("n/a") is None


def test_normalize_product_compacta_y_respeta_stock():
    p = normalize_product(RAW)
    assert p["producto_id"] == "254803098"
    assert p["nombre"] == "DoyPack zipper Papel x 50"
    assert p["descripcion"] == "Cierre zipper & fuelle"  # sin HTML
    v1, v2, v3 = p["variantes"]
    assert v1["opcion"] == "18 x 26 / Papel"
    assert v1["precio"] == "$32.161,09"
    assert v1["precio_promocional"] == "$29.000,00"
    assert v1["unidades_disponibles"] == 5 and v1["en_stock"] is True
    assert v2["en_stock"] is False  # stock 0 gestionado
    assert v3["en_stock"] is True and "unidades_disponibles" not in v3  # ilimitado
    assert p["disponible"] is True


def test_normalize_producto_sin_stock_total():
    raw = dict(RAW, variants=[RAW["variants"][1]])
    assert normalize_product(raw)["disponible"] is False


@pytest.fixture
async def client():
    c = TiendanubeClient("777", "tok-secreto", user_agent="Test (t@t.com)")
    yield c
    await c.aclose()


async def test_search_ok_envia_auth_y_filtros(client, respx_mock):
    route = respx_mock.get(f"{BASE}/products").mock(
        return_value=httpx.Response(200, json=[RAW])
    )
    out = await client.search("doypack", limit=3)
    assert out[0]["nombre"] == "DoyPack zipper Papel x 50"
    req = route.calls.last.request
    assert req.headers["Authentication"] == "bearer tok-secreto"
    assert req.headers["User-Agent"] == "Test (t@t.com)"
    assert req.url.params["q"] == "doypack"
    assert req.url.params["published"] == "true"
    assert req.url.params["per_page"] == "3"


async def test_search_404_es_sin_resultados(client, respx_mock):
    respx_mock.get(f"{BASE}/products").mock(
        return_value=httpx.Response(404, json={"code": 404, "description": "Last page is 0"})
    )
    assert await client.search("mochila") == []


async def test_search_reintenta_con_palabra_mas_larga(client, respx_mock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "doypack":
            return httpx.Response(200, json=[RAW])
        return httpx.Response(404, json={})

    respx_mock.get(f"{BASE}/products").mock(side_effect=handler)
    out = await client.search("quiero doypack zipper")  # frase completa → 404
    assert len(out) == 1


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_errores_http_levantan_tiendanube_error(client, respx_mock, status):
    respx_mock.get(f"{BASE}/products").mock(return_value=httpx.Response(status))
    with pytest.raises(TiendanubeError):
        await client.search("doypack")


async def test_get_producto_detalle(client, respx_mock):
    respx_mock.get(f"{BASE}/products/254803098").mock(
        return_value=httpx.Response(200, json=RAW)
    )
    p = await client.get("254803098")
    assert p is not None and p["etiquetas"] == ["DoyPacks"]
    respx_mock.get(f"{BASE}/products/1").mock(return_value=httpx.Response(404))
    assert await client.get("1") is None


# --- herramientas -----------------------------------------------------------


async def _runtime(tn):
    ctx = make_ctx(make_settings(agent_mode="ventas"))
    ctx.tiendanube = tn
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    return ToolRuntime(ctx, conv, CRM_CONV_ID), ctx


async def test_tool_buscar_productos(client, respx_mock):
    respx_mock.get(f"{BASE}/products").mock(return_value=httpx.Response(200, json=[RAW]))
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_productos", {"consulta": "doypack"})
    assert res["ok"] is True and res["productos"][0]["producto_id"] == "254803098"
    await ctx.crm.aclose()


async def test_tool_sin_resultados_y_consulta_vacia(client, respx_mock):
    respx_mock.get(f"{BASE}/products").mock(return_value=httpx.Response(404))
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_productos", {"consulta": "mochila"})
    assert res["ok"] is True and res["productos"] == []
    assert (await runtime.execute("buscar_productos", {"consulta": " "}))["ok"] is False
    await ctx.crm.aclose()


async def test_tool_catalogo_caido_no_tumba_el_turno(client, respx_mock):
    respx_mock.get(f"{BASE}/products").mock(return_value=httpx.Response(500))
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("buscar_productos", {"consulta": "doypack"})
    assert res["ok"] is False and res["error"] == "catalogo_no_disponible"
    assert "no inventes" in res["detalle"]
    await ctx.crm.aclose()


async def test_tool_ver_producto_valida_id(client):
    runtime, ctx = await _runtime(client)
    res = await runtime.execute("ver_producto", {"producto_id": "../admin"})
    assert res["ok"] is False and res["error"] == "producto_id_invalido"
    await ctx.crm.aclose()


async def test_tool_sin_cliente_configurado():
    runtime, ctx = await _runtime(None)
    res = await runtime.execute("buscar_productos", {"consulta": "x"})
    assert res["error"] == "catalogo_no_disponible"
    await ctx.crm.aclose()


# --- modos ------------------------------------------------------------------


def _names(tools):
    return {t["function"]["name"] for t in tools}


def test_tool_schemas_por_modo():
    base = _names(tool_schemas(make_settings()))
    assert {"propose_slots", "book_session"} <= base
    assert "buscar_productos" not in base  # default intacto
    ventas = _names(tool_schemas(make_settings(agent_mode="ventas")))
    assert ventas == {"update_ficha", "handoff", "buscar_productos", "ver_producto"}


def test_prompt_ventas_vs_agendamiento():
    conv = Conversation(id=1, wa_identity=IDENTITY, greeted=True)
    ag = build_system_prompt(profile=BusinessProfile(), context=None, conv=conv)
    vt = build_system_prompt(
        profile=BusinessProfile(), context=None, conv=conv, mode="ventas"
    )
    assert "propose_slots" in ag and "buscar_productos" not in ag
    assert "propose_slots" not in vt and "book_session" not in vt
    assert "buscar_productos" in vt
    # los NUNCA de seguridad se conservan en ambos modos
    for text in (ag, vt):
        assert "hostilidad" in text
        assert "MULTIMEDIA" in text
    assert "Jamás afirmes que hay stock" in vt
    assert "Limítate a lo que muestre el catálogo" in vt


# --- turno completo (cliente → catálogo → respuesta) --------------------------


async def test_turno_ventas_consulta_catalogo_y_responde(respx_mock):
    import asyncio
    import json

    from app.llm import LlmReply, ToolCall
    from app.main import create_app
    from tests.conftest import FakeLLM, mock_crm_basics, wa_body

    llm = FakeLLM(
        [
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(id="t1", name="buscar_productos", arguments={"consulta": "doypack"})
                ],
            ),
            LlmReply(content="Te sirve el DoyPack papel 18x26: $32.161,09 por 50 unidades."),
        ]
    )
    ctx = make_ctx(make_settings(agent_mode="ventas"), llm=llm)
    tn = TiendanubeClient("777", "tok")
    ctx.tiendanube = tn
    catalog = respx_mock.get(f"{BASE}/products").mock(
        return_value=httpx.Response(200, json=[RAW])
    )
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as http:
        await http.post("/webhook", content=wa_body(text="necesito doypacks"))
        await asyncio.sleep(0.3)

    assert catalog.call_count == 1  # consultó el catálogo real
    sent = json.loads(routes["messages"].calls[0].request.content)
    assert "$32.161,09" in json.dumps(sent, ensure_ascii=False)
    # el LLM vio SOLO las herramientas del modo ventas, y el resultado del catálogo
    first_tools = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "buscar_productos" in first_tools and "book_session" not in first_tools
    tool_msg = next(m for m in llm.calls[1]["messages"] if m["role"] == "tool")
    assert "254803098" in tool_msg["content"]
    await tn.aclose()
    await ctx.crm.aclose()
