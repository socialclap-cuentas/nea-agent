"""Pausa global, panel /admin, vencimiento de respuestas viejas y etapa al derivar."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta

import httpx
import pytest

from app.control import is_paused, set_paused
from app.followup import FollowupWorker
from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.sender import SenderWorker
from app.state import utcnow
from app.tools import ToolRuntime, tool_schemas
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    FakeLLM,
    make_ctx,
    make_settings,
    mock_crm_basics,
    wa_body,
)


def _auth(token: str = "s3cret") -> dict[str, str]:
    raw = base64.b64encode(f"admin:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
async def admin_client():
    ctx = make_ctx(make_settings(admin_token="s3cret"))
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as c:
        yield c, ctx
    await ctx.crm.aclose()


# --- pausa ------------------------------------------------------------------


async def test_pausa_se_guarda_y_se_levanta():
    ctx = make_ctx()
    assert await is_paused(ctx) is False
    await set_paused(ctx, True)
    assert await is_paused(ctx) is True
    await set_paused(ctx, False)
    assert await is_paused(ctx) is False
    await ctx.crm.aclose()


async def test_pausa_por_entorno_manda_sobre_todo():
    ctx = make_ctx(make_settings(agent_paused=True))
    assert await is_paused(ctx) is True
    await ctx.crm.aclose()


async def test_pausado_no_responde_pero_el_relay_sigue(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    await set_paused(ctx, True)
    resp = await client.post("/webhook", content=wa_body(text="hola"))
    assert resp.status_code == 200
    await asyncio.sleep(0.25)
    assert routes["messages"].call_count == 0  # el agente calla
    assert len(ctx.store.relays) == 1  # la Bandeja del CRM sí recibe el mensaje


async def test_reactivado_vuelve_a_responder(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    await set_paused(ctx, True)
    await set_paused(ctx, False)
    await client.post("/webhook", content=wa_body(text="hola"))
    await asyncio.sleep(0.25)
    assert routes["messages"].call_count == 1


async def test_pausado_no_manda_seguimientos_ni_reintentos(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "texto")
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    await set_paused(ctx, True)
    await SenderWorker(ctx).tick()
    await FollowupWorker(ctx).tick()
    assert messages.call_count == 0
    assert ctx.store.pending_sends[pid].delivered_at is None
    await ctx.crm.aclose()


# --- respuestas viejas ----------------------------------------------------------


async def test_respuesta_vieja_se_descarta_sin_handoff(respx_mock):
    ctx = make_ctx()  # vencimiento por defecto: 20 min
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "Hola!")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=45)
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={})
    )
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    await SenderWorker(ctx).tick()
    assert messages.call_count == 0  # no se manda: ya está vieja
    assert ctx.store.pending_sends[pid].abandoned_at is not None
    assert handoff.call_count == 0  # y no se pausa la conversación por esto
    await ctx.crm.aclose()


async def test_respuesta_reciente_si_se_entrega(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    pid = await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "Hola!")
    ctx.store.pending_sends[pid].created_at = utcnow() - timedelta(minutes=5)
    messages = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={})
    )
    await SenderWorker(ctx).tick()
    assert messages.call_count == 1
    await ctx.crm.aclose()


# --- panel ------------------------------------------------------------------


async def test_panel_deshabilitado_sin_token(client):
    assert (await client.get("/admin")).status_code == 404


async def test_panel_exige_credencial(admin_client):
    c, _ = admin_client
    assert (await c.get("/admin")).status_code == 401
    assert (await c.get("/admin", headers=_auth("mala"))).status_code == 401
    assert (await c.post("/admin/pausar")).status_code == 401  # ni pausa sin clave


async def test_panel_pausa_y_reactiva(admin_client):
    c, ctx = admin_client
    page = await c.get("/admin", headers=_auth())
    assert "ACTIVO" in page.text
    r = await c.post("/admin/pausar", headers=_auth())
    assert r.status_code == 303 and await is_paused(ctx) is True
    assert "PAUSADO" in (await c.get("/admin", headers=_auth())).text
    await c.post("/admin/reactivar", headers=_auth())
    assert await is_paused(ctx) is False


async def test_panel_vacia_cola(admin_client):
    c, ctx = admin_client
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    for _ in range(3):
        await ctx.store.enqueue_pending_send(conv.id, CRM_CONV_ID, "x")
    r = await c.post("/admin/vaciar-cola", headers=_auth())
    assert r.status_code == 303 and "3+respuestas" in r.headers["location"]
    assert await ctx.store.due_pending_sends(utcnow() + timedelta(days=1)) == []


# --- derivar a una etapa del pipeline ----------------------------------------


def test_handoff_expone_etapas_configuradas_en_ventas():
    tools = tool_schemas(make_settings(agent_mode="ventas"))
    handoff = next(t for t in tools if t["function"]["name"] == "handoff")
    etapa = handoff["function"]["parameters"]["properties"]["etapa"]
    assert set(etapa["enum"]) == {"Reclamo", "Seguimiento de pedido", "Presupuesto"}
    # y en agendamiento el handoff no cambia
    base = next(t for t in tool_schemas(make_settings()) if t["function"]["name"] == "handoff")
    assert "etapa" not in base["function"]["parameters"]["properties"]


async def test_handoff_ignora_etapa_inventada():
    ctx = make_ctx(make_settings(agent_mode="ventas"))
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    rt = ToolRuntime(ctx, conv, CRM_CONV_ID)
    await rt.execute("handoff", {"reason": "x", "etapa": "Cliente VIP inventada"})
    assert rt.handoff_reason == "x" and rt.handoff_stage is None
    await rt.execute("handoff", {"reason": "x", "etapa": "Reclamo"})
    assert rt.handoff_stage == "Reclamo"
    await ctx.crm.aclose()


async def test_turno_deriva_y_mueve_la_etapa_antes_de_pausar(respx_mock):
    llm = FakeLLM(
        [
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="handoff",
                        arguments={"reason": "reclamo", "etapa": "Reclamo"},
                    )
                ],
            ),
            LlmReply(content="Lamento eso. Te paso con una persona del equipo que lo resuelve."),
        ]
    )
    ctx = make_ctx(make_settings(agent_mode="ventas"), llm=llm)
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as http:
        await http.post("/webhook", content=wa_body(text="me llegó roto el pedido"))
        await asyncio.sleep(0.3)
    paths = [str(c.request.url.path) for c in respx_mock.calls if "/api/bot/" in str(c.request.url)]
    # orden: despedida → etapa (ficha) → handoff (que pausa la IA)
    assert paths.index("/api/bot/messages") < paths.index("/api/bot/ficha") < paths.index("/api/bot/handoff")
    body = json.loads(routes["ficha"].calls[-1].request.content)
    assert body["stage"] == "Reclamo"
    await ctx.crm.aclose()
