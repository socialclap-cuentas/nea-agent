"""070 — Vertical salud: herramientas, prompt y turnos de punta a punta.

Lo que se fija:
- sin el bloque `salud` del CRM, NADA cambia (ni herramientas ni prompt);
- con él, el agente ofrece con quién es cada horario, reserva con profesional y
  tipo, cancela solo con confirmación citable y deriva las urgencias;
- el camino infeliz (el CRM caído, un id inventado, falta de confirmación)
  degrada sin colgar el turno ni mandar texto roto.
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.llm import LlmReply, ToolCall
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation, OfferedSlot
from app.tools import (
    SALUD_TOOLS,
    ToolRuntime,
    con_salud,
    hay_salud,
    solo_encendidas,
    tool_schemas,
)
from app.turn import _sembrar_hueco_de_espera
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    crm_context,
    despachar,
    make_ctx,
    mock_crm_basics,
)

SLOT_DT = datetime(2026, 12, 10, 15, 0, tzinfo=timezone.utc)
SLOT_ISO = "2026-12-10T15:00:00Z"

SALUD_BLOCK = {
    "nextBooking": None,
    "surveyPending": False,
    "waitlist": None,
    "openUrgency": False,
    "patient": {"contactId": "ct_1", "documentNumber": None, "insuranceId": None},
}


def nombres(schemas):
    return [t["function"]["name"] for t in schemas]


# ------------------------------------------------------------ herramientas ---


def test_sin_salud_el_catalogo_es_el_de_siempre():
    base = tool_schemas()
    assert con_salud(base, False) is base
    assert not (set(nombres(base)) & SALUD_TOOLS)


def test_con_salud_suma_herramientas_y_parametros_sin_tocar_el_original():
    base = tool_schemas()
    antes = copy.deepcopy(base)
    out = con_salud(base, True)
    assert base == antes  # no muta el catálogo compartido
    assert SALUD_TOOLS <= set(nombres(out))
    por_nombre = {t["function"]["name"]: t["function"] for t in out}
    props = por_nombre["propose_slots"]["parameters"]["properties"]
    assert {"professional_id", "appointment_type_id", "insurance_id"} <= set(props)
    assert {"appointment_type_id", "insurance_id"} <= set(
        por_nombre["book_session"]["parameters"]["properties"]
    )
    # book_session sigue exigiendo lo de siempre
    assert por_nombre["book_session"]["parameters"]["required"] == ["start_utc", "dia_confirmado"]


def test_sin_agenda_solo_catalogo_urgencia_y_ficha():
    sin_agenda = tool_schemas(agenda_enabled=False)
    out = nombres(con_salud(sin_agenda, True))
    assert "urgencia" in out and "salud_catalogo" in out and "guardar_paciente" in out
    assert "cancelar_turno" not in out and "lista_de_espera" not in out


def test_la_urgencia_no_se_puede_apagar_desde_el_perfil():
    # El negocio apagó todo menos lo esencial: la red de seguridad queda.
    perfil = BusinessProfile(tools=[])
    out = nombres(solo_encendidas(con_salud(tool_schemas(), True), perfil))
    assert "urgencia" in out and "handoff" in out
    assert "cancelar_turno" not in out  # el resto del consultorio sí se apaga


def test_hay_salud_solo_con_el_bloque():
    assert hay_salud({"salud": SALUD_BLOCK}) is True
    assert hay_salud({"salud": None}) is False
    assert hay_salud({}) is False
    assert hay_salud(None) is False


# ---------------------------------------------------------------- runtime ---


@pytest.fixture
async def rt():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    # Con documento ya guardado: la obra social la aporta cada prueba.
    runtime = ToolRuntime(
        ctx, conv, CRM_CONV_ID, salud=True, paciente={"documentNumber": "30111222"}
    )
    yield runtime, ctx, conv
    await ctx.crm.aclose()


@pytest.fixture
async def rt_sin_datos():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID, salud=True, paciente=SALUD_BLOCK["patient"])
    yield runtime, ctx, conv
    await ctx.crm.aclose()


async def _ofrecer(ctx, conv):
    await ctx.store.replace_offered_slots(
        conv.id,
        [
            OfferedSlot(
                conversation_id=conv.id,
                start_utc=SLOT_DT,
                end_utc=None,
                label="jueves 10 de diciembre, 12:00",
                professional_id="pro_a",
                professional_name="Dra. Pérez",
                appointment_type_id="apt_1",
            )
        ],
    )


async def test_book_session_no_reserva_sin_documento_ni_obra_social(rt_sin_datos, respx_mock):
    runtime, ctx, conv = rt_sin_datos
    await _ofrecer(ctx, conv)
    reserva = respx_mock.post(f"{CRM_URL}/api/bot/bookings")
    r = await runtime.execute(
        "book_session", {"start_utc": SLOT_ISO, "dia_confirmado": "sí, el jueves"}
    )
    assert r["ok"] is False
    assert r["error"] == "faltan_datos_del_paciente"
    assert len(r["faltan"]) == 2
    assert not reserva.called
    assert runtime.booked is False


async def test_book_session_reserva_tras_guardar_datos_en_el_mismo_turno(rt_sin_datos, respx_mock):
    runtime, ctx, conv = rt_sin_datos
    await _ofrecer(ctx, conv)
    respx_mock.put(f"{CRM_URL}/api/bot/salud/paciente").mock(return_value=httpx.Response(200, json={}))
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(
        return_value=httpx.Response(200, json={"rules": {}})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(201, json={"bookingId": "bk_1", "label": "x", "linkPending": False})
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(return_value=httpx.Response(200, json={}))
    g = await runtime.execute(
        "guardar_paciente", {"documento": "30111222", "obra_social_id": "ins_1"}
    )
    assert g["ok"] is True
    r = await runtime.execute(
        "book_session", {"start_utc": SLOT_ISO, "dia_confirmado": "sí, el jueves"}
    )
    assert r["ok"] is True and runtime.booked is True


async def test_book_session_acepta_particular_citado(rt, respx_mock):
    runtime, ctx, conv = rt
    await _ofrecer(ctx, conv)
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(
        return_value=httpx.Response(200, json={"rules": {}})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(201, json={"bookingId": "bk_1", "label": "x", "linkPending": False})
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(return_value=httpx.Response(200, json={}))
    r = await runtime.execute(
        "book_session",
        {"start_utc": SLOT_ISO, "dia_confirmado": "sí, el jueves", "sin_obra_social": "voy particular"},
    )
    assert r["ok"] is True


async def test_propose_slots_reenvia_profesional_tipo_y_obra_social(rt, respx_mock):
    runtime, ctx, conv = rt
    ruta = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": [
                    {
                        "startUtc": SLOT_ISO,
                        "label": "jueves 10 de diciembre, 12:00",
                        "professionalId": "pro_a",
                        "professionalName": "Dra. Pérez",
                        "appointmentTypeId": "apt_1",
                    }
                ],
                "query": None,
            },
        )
    )
    r = await runtime.execute(
        "propose_slots",
        {"professional_id": "pro_a", "appointment_type_id": "apt_1", "insurance_id": "ins_1"},
    )
    assert r["ok"] is True
    params = dict(ruta.calls[0].request.url.params)
    assert params["professionalId"] == "pro_a"
    assert params["appointmentTypeId"] == "apt_1"
    assert params["insuranceId"] == "ins_1"
    # el modelo ve con quién es cada horario
    assert r["slots"][0]["profesional"] == "Dra. Pérez"
    # y la oferta local lo recuerda (persistible)
    guardado = (await ctx.store.get_offered_slots(conv.id))[0]
    assert guardado.professional_id == "pro_a"
    assert guardado.appointment_type_id == "apt_1"


async def test_propose_slots_sin_salud_no_manda_parametros_nuevos(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)  # salud=False
    ruta = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            200, json={"slots": [{"startUtc": SLOT_ISO, "label": "jueves 10, 12:00"}], "query": None}
        )
    )
    await runtime.execute("propose_slots", {"professional_id": "pro_a"})
    assert set(dict(ruta.calls[0].request.url.params)) <= {
        "conversationId", "limit", "perDay", "days"
    }
    await ctx.crm.aclose()


async def test_propose_slots_con_id_inventado_devuelve_error_legible(rt, respx_mock):
    runtime, *_ = rt
    respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            422, json={"error": {"code": "invalid_body", "message": "Profesional no encontrado"}}
        )
    )
    r = await runtime.execute("propose_slots", {"professional_id": "pro_fantasma"})
    assert r["ok"] is False
    assert r["error"] == "parametro_invalido"
    assert "Profesional no encontrado" in r["detalle"]
    assert "salud_catalogo" in r["detalle"]


async def test_book_session_reserva_con_el_profesional_del_horario_ofrecido(rt, respx_mock):
    runtime, ctx, conv = rt
    await ctx.store.replace_offered_slots(
        conv.id,
        [
            OfferedSlot(
                conversation_id=conv.id,
                start_utc=SLOT_DT,
                end_utc=None,
                label="jueves 10 de diciembre, 12:00",
                professional_id="pro_a",
                professional_name="Dra. Pérez",
                appointment_type_id="apt_1",
            )
        ],
    )
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(
        return_value=httpx.Response(200, json={"rules": {"depositPaymentInfo": None}})
    )
    reserva = respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={
                "bookingId": "bk_1",
                "label": "jueves 10 de diciembre, 12:00",
                "meetingLink": None,
                "linkPending": False,
                "depositAmount": 5000,
                "depositStatus": "pendiente",
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}, "stageMoved": False})
    )
    r = await runtime.execute(
        "book_session",
        {"start_utc": SLOT_ISO, "dia_confirmado": "sí, el jueves", "insurance_id": "ins_1"},
    )
    assert r["ok"] is True
    body = json.loads(reserva.calls[0].request.content)
    assert body == {
        "conversationId": CRM_CONV_ID,
        "startUtc": SLOT_ISO,
        "professionalId": "pro_a",
        "appointmentTypeId": "apt_1",
        "insuranceId": "ins_1",
    }
    assert r["profesional"] == "Dra. Pérez"
    assert r["sena"]["monto"] == 5000
    assert "depositPaymentInfo" in r["sena"]["instrucciones"]
    assert "No inventes" in r["sena"]["instrucciones"]


async def test_cancelar_turno_exige_confirmacion_citable(rt, respx_mock):
    runtime, *_ = rt
    cancelar = respx_mock.post(f"{CRM_URL}/api/bot/salud/turno/cancelar").mock(
        return_value=httpx.Response(200, json={"ok": True, "bookingId": "bk_1"})
    )
    # sin bandera
    r1 = await runtime.execute("cancelar_turno", {"confirmado": False, "frase_del_paciente": "mmm"})
    # con bandera pero sin frase que la sostenga
    r2 = await runtime.execute("cancelar_turno", {"confirmado": True, "frase_del_paciente": "  "})
    for r in (r1, r2):
        assert r["ok"] is False and r["error"] == "confirmacion_requerida"
    assert cancelar.call_count == 0  # jamás llegó al CRM
    assert runtime.turno_cancelado is False

    r3 = await runtime.execute(
        "cancelar_turno", {"confirmado": True, "frase_del_paciente": "sí, cancelalo"}
    )
    assert r3["ok"] is True and runtime.turno_cancelado is True
    assert json.loads(cancelar.calls[0].request.content) == {
        "conversationId": CRM_CONV_ID,
        "confirmed": True,
    }


async def test_cancelar_sin_turno_devuelve_el_motivo_del_crm(rt, respx_mock):
    runtime, *_ = rt
    respx_mock.post(f"{CRM_URL}/api/bot/salud/turno/cancelar").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "No tiene un turno próximo"}}
        )
    )
    r = await runtime.execute(
        "cancelar_turno", {"confirmado": True, "frase_del_paciente": "sí"}
    )
    assert r["ok"] is False and r["error"] == "not_found"
    assert "No tiene un turno próximo" in r["detalle"]
    assert runtime.turno_cancelado is False


async def test_urgencia_registra_y_trae_el_mensaje_del_consultorio(rt, respx_mock):
    runtime, *_ = rt
    ruta = respx_mock.post(f"{CRM_URL}/api/bot/salud/urgencia").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "triageId": "tri_1", "level": "urgente", "message": "Llamá al 107."},
        )
    )
    r = await runtime.execute(
        "urgencia", {"nivel": "URGENTE", "motivo": "dolor de pecho y falta de aire"}
    )
    assert r["ok"] is True and r["mensaje_para_el_paciente"] == "Llamá al 107."
    assert "NO diagnostiques" in r["instrucciones"]
    assert runtime.urgencia == {"nivel": "urgente", "mensaje": "Llamá al 107."}
    assert json.loads(ruta.calls[0].request.content) == {
        "conversationId": CRM_CONV_ID,
        "level": "urgente",
        "reason": "dolor de pecho y falta de aire",
    }


async def test_urgencia_con_nivel_raro_cuenta_como_urgente(rt, respx_mock):
    runtime, *_ = rt
    ruta = respx_mock.post(f"{CRM_URL}/api/bot/salud/urgencia").mock(
        return_value=httpx.Response(200, json={"ok": True, "level": "urgente", "message": "x"})
    )
    await runtime.execute("urgencia", {"nivel": "grave??", "motivo": "m"})
    assert json.loads(ruta.calls[0].request.content)["level"] == "urgente"


async def test_encuesta_valida_el_puntaje_y_pasa_el_link_de_resena(rt, respx_mock):
    runtime, *_ = rt
    ruta = respx_mock.post(f"{CRM_URL}/api/bot/salud/encuesta").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "score": 5, "reviewUrl": "https://g.page/r/abc"}
        )
    )
    malo = await runtime.execute("registrar_encuesta", {"puntaje": "excelente"})
    assert malo["ok"] is False and malo["error"] == "puntaje_invalido"
    assert ruta.call_count == 0
    r = await runtime.execute("registrar_encuesta", {"puntaje": "5", "comentario": "genial"})
    assert r["ok"] is True and r["link_de_resena"] == "https://g.page/r/abc"
    assert json.loads(ruta.calls[0].request.content)["score"] == 5


async def test_guardar_paciente_mapea_campos_y_no_llama_sin_datos(rt, respx_mock):
    runtime, *_ = rt
    ruta = respx_mock.put(f"{CRM_URL}/api/bot/salud/paciente").mock(
        return_value=httpx.Response(200, json={"patient": {}})
    )
    vacio = await runtime.execute("guardar_paciente", {})
    assert vacio["ok"] is True and ruta.call_count == 0
    await runtime.execute(
        "guardar_paciente",
        {
            "documento": "30123456",
            "fecha_nacimiento": "1990-05-17",
            "obra_social_id": "ins_1",
            "numero_afiliado": "A-99",
            "no_enviar_seguimientos": True,
            "sintomas": "no debería viajar",
        },
    )
    assert json.loads(ruta.calls[0].request.content) == {
        "conversationId": CRM_CONV_ID,
        "documentNumber": "30123456",
        "birthDate": "1990-05-17",
        "insuranceId": "ins_1",
        "affiliateNumber": "A-99",
        "optOutFollowups": True,
    }


async def test_el_crm_caido_degrada_sin_romper_la_herramienta(rt, respx_mock):
    runtime, *_ = rt
    respx_mock.post(f"{CRM_URL}/api/bot/salud/urgencia").mock(
        return_value=httpx.Response(503, json={})
    )
    r = await runtime.execute("urgencia", {"nivel": "urgente", "motivo": "m"})
    assert r["ok"] is False and r["error"] == "crm_error"
    assert runtime.urgencia is None


async def test_herramientas_de_salud_inexistentes_sin_el_bloque(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)  # salud=False
    r = await runtime.execute("urgencia", {"nivel": "urgente", "motivo": "m"})
    assert r["ok"] is False and "desconocida" in r["error"]
    await ctx.crm.aclose()


# ----------------------------------------------------------------- prompt ---


def _prompt(context):
    return build_system_prompt(
        profile=BusinessProfile(),
        context=context,
        conv=Conversation(id=1, wa_identity=IDENTITY, greeted=True),
        now=datetime(2026, 12, 8, 15, 0, tzinfo=timezone.utc),
    )


def test_el_prompt_sin_salud_es_el_de_siempre():
    p = _prompt({"contact": {"name": "Ana"}})
    assert "CONSULTORIO" not in p and "urgencia" not in p.split("CONTEXTO ACTUAL:")[1]


def test_el_prompt_con_salud_trae_el_protocolo_y_no_toca_el_chasis():
    sin = _prompt({"contact": {"name": "Ana"}})
    con = _prompt({"contact": {"name": "Ana"}, "salud": SALUD_BLOCK})
    chasis_sin = sin.split("CONTEXTO ACTUAL:")[0]
    chasis_con = con.split("CONTEXTO ACTUAL:")[0]
    assert chasis_sin == chasis_con  # los NUNCA siguen intactos
    bloque = con.split("CONTEXTO ACTUAL:")[1]
    assert "NUNCA diagnostiques" in bloque
    assert "herramienta urgencia" in bloque
    assert "salud_catalogo" in bloque
    assert "cancelar_turno" in bloque


def test_el_prompt_refleja_turno_sena_encuesta_y_espera():
    salud = {
        **SALUD_BLOCK,
        "nextBooking": {
            "label": "jueves 10 de diciembre, 12:00",
            "professional": {"id": "pro_a", "name": "Dra. Pérez"},
            "appointmentType": {"id": "apt_1", "name": "Limpieza", "prepNotes": "Traé tu DNI"},
            "confirmation": "pendiente",
            "depositStatus": "pendiente",
            "depositAmount": 5000,
            "reminderSent": True,
        },
        "surveyPending": True,
        "waitlist": {"status": "offered", "offeredLabel": "viernes 11, 09:00"},
    }
    b = _prompt({"contact": {"name": "Ana"}, "salud": salud}).split("CONTEXTO ACTUAL:")[1]
    assert "Dra. Pérez" in b and "Limpieza" in b
    assert "Traé tu DNI" in b
    assert "SEÑA PENDIENTE de 5000" in b
    assert "confirmar_turno" in b  # respuesta a un recordatorio
    assert "registrar_encuesta" in b
    assert "viernes 11, 09:00" in b and "book_session" in b
    assert "documento" in b  # datos que faltan


def test_con_urgencia_abierta_no_se_sigue_agendando():
    b = _prompt({"contact": {}, "salud": {**SALUD_BLOCK, "openUrgency": True}}).split(
        "CONTEXTO ACTUAL:"
    )[1]
    assert "URGENCIA abierta" in b and "No agendes" in b


# ------------------------------------------------ hueco de lista de espera ---


async def test_el_hueco_de_la_lista_de_espera_se_siembra_si_no_hay_oferta_propia():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    contexto = {
        "salud": {
            **SALUD_BLOCK,
            "waitlist": {
                "status": "offered",
                "offeredLabel": "x",
                "offered": {
                    "startUtc": SLOT_ISO,
                    "label": "jueves 10 de diciembre, 12:00",
                    "professionalId": "pro_a",
                    "professionalName": "Dra. Pérez",
                    "appointmentTypeId": None,
                },
            },
        }
    }
    await _sembrar_hueco_de_espera(ctx, conv.id, contexto)
    sembrado = await ctx.store.get_offered_slots(conv.id)
    assert len(sembrado) == 1 and sembrado[0].professional_id == "pro_a"

    # una oferta propia ya presente NO se pisa
    propia = OfferedSlot(
        conversation_id=conv.id,
        start_utc=datetime(2026, 12, 12, 10, 0, tzinfo=timezone.utc),
        end_utc=None,
        label="sábado 12, 07:00",
    )
    await ctx.store.replace_offered_slots(conv.id, [propia])
    await _sembrar_hueco_de_espera(ctx, conv.id, contexto)
    assert [s.label for s in await ctx.store.get_offered_slots(conv.id)] == ["sábado 12, 07:00"]
    await ctx.crm.aclose()


async def test_sembrar_sin_bloque_o_con_datos_rotos_no_falla():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    for contexto in (None, {}, {"salud": {"waitlist": {"offered": "roto"}}},
                     {"salud": {"waitlist": {"offered": {"startUtc": "no-es-fecha"}}}}):
        await _sembrar_hueco_de_espera(ctx, conv.id, contexto)
    assert await ctx.store.get_offered_slots(conv.id) == []
    await ctx.crm.aclose()


# ---------------------------------------- turnos de punta a punta (E2E) -----


def _contexto_salud(**salud):
    c = crm_context()
    c["salud"] = {**SALUD_BLOCK, **salud}
    return c


def _rutas(respx_mock, **salud):
    routes = mock_crm_basics(respx_mock)
    routes["context"].mock(return_value=httpx.Response(200, json=_contexto_salud(**salud)))
    routes["availability"] = respx_mock.get(f"{CRM_URL}/api/bot/availability").mock(
        return_value=httpx.Response(
            200,
            json={
                "slots": [
                    {
                        "startUtc": SLOT_ISO,
                        "label": "jueves 10 de diciembre, 12:00",
                        "professionalId": "pro_a",
                        "professionalName": "Dra. Pérez",
                        "appointmentTypeId": "apt_1",
                    }
                ],
                "query": None,
            },
        )
    )
    return routes


def _enviados(routes):
    return [json.loads(c.request.content)["text"] for c in routes["messages"].calls]


async def test_e2e_urgencia_se_deriva_sin_diagnosticar(ctx, client, respx_mock):
    routes = _rutas(respx_mock)
    urgencia = respx_mock.post(f"{CRM_URL}/api/bot/salud/urgencia").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "triageId": "tri_1",
                "level": "urgente",
                "message": "Te paso con una persona ya. Si es una emergencia, llamá al 107.",
            },
        )
    )
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="urgencia",
                    arguments={"nivel": "urgente", "motivo": "dolor fuerte en el pecho"},
                )
            ],
        ),
        LlmReply(content="Entiendo, es importante. Ya aviso al equipo para que te atienda ahora; si empeora, llamá al 107."),
    ]
    await despachar(client, ctx, text="me duele muchísimo el pecho y me falta el aire", esperar=True)
    await asyncio.sleep(0.1)

    assert urgencia.call_count == 1
    assert json.loads(urgencia.calls[0].request.content)["level"] == "urgente"
    enviados = _enviados(routes)
    assert len(enviados) == 1 and "107" in enviados[0]
    # En ESA ronda el modelo vio la herramienta (con el bloque salud en el contexto)
    assert "urgencia" in nombres(ctx.llm.calls[0]["tools"])
    # y el prompt le prohíbe diagnosticar
    assert "NUNCA diagnostiques" in ctx.llm.calls[0]["messages"][0]["content"]


async def test_e2e_reserva_con_profesional_tipo_y_sena(ctx, client, respx_mock):
    routes = _rutas(respx_mock, patient={"contactId": "ct_1", "documentNumber": "30111222", "insuranceId": None})
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(
        return_value=httpx.Response(
            200, json={"rules": {"depositPaymentInfo": "Alias: clinica.pagos"}}
        )
    )
    reserva = respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={
                "bookingId": "bk_1",
                "label": "jueves 10 de diciembre, 12:00",
                "meetingLink": None,
                "linkPending": False,
                "depositAmount": 5000,
                "depositStatus": "pendiente",
            },
        )
    )
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="propose_slots",
                    arguments={"appointment_type_id": "apt_1", "insurance_id": "ins_1"},
                )
            ],
        ),
        LlmReply(content="Tengo el jueves 10 a las 12:00 con la Dra. Pérez. ¿Te sirve?"),
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(
                    id="t2",
                    name="book_session",
                    arguments={
                        "start_utc": SLOT_ISO,
                        "dia_confirmado": "sí, el jueves 10 a las 12",
                        "appointment_type_id": "apt_1",
                        "insurance_id": "ins_1",
                    },
                )
            ],
        ),
        LlmReply(content="Listo, quedó tu turno del jueves 10 a las 12:00 con la Dra. Pérez. Tiene una seña de $5000."),
    ]
    await despachar(client, ctx, text="quiero turno para una limpieza, tengo OSDE", esperar=True)
    await asyncio.sleep(0.1)
    # segunda ronda de conversación: el paciente acepta el horario
    await despachar(client, ctx, text="sí, el jueves 10 a las 12", wamid="wamid.002", esperar=True)
    await asyncio.sleep(0.1)

    # disponibilidad pedida con tipo y obra social
    params = dict(routes["availability"].calls[0].request.url.params)
    assert params["appointmentTypeId"] == "apt_1" and params["insuranceId"] == "ins_1"
    # la reserva viaja con el profesional del horario que se ofreció
    cuerpo = json.loads(reserva.calls[0].request.content)
    assert cuerpo["professionalId"] == "pro_a"
    assert cuerpo["appointmentTypeId"] == "apt_1"
    assert cuerpo["insuranceId"] == "ins_1"
    # el modelo recibió la instrucción de la seña (sin inventar cómo pagarla)
    vistos = json.dumps(ctx.llm.calls[-1]["messages"], ensure_ascii=False)
    assert "rules.depositPaymentInfo" in vistos
    # La confirmación del turno es autoritativa: sale con el profesional y la
    # seña, y con el cómo-pagar REAL del consultorio (no el que invente el modelo).
    final = _enviados(routes)[-1]
    assert "con Dra. Pérez" in final
    assert "seña de $5000" in final
    assert "Alias: clinica.pagos" in final


async def test_e2e_cancelar_sin_confirmar_no_cancela(ctx, client, respx_mock):
    routes = _rutas(
        respx_mock,
        nextBooking={
            "id": "bk_1",
            "label": "jueves 10 de diciembre, 12:00",
            "professional": {"id": "pro_a", "name": "Dra. Pérez"},
            "appointmentType": None,
            "confirmation": "pendiente",
            "depositStatus": "no_aplica",
            "depositAmount": None,
            "reminderSent": True,
        },
    )
    cancelar = respx_mock.post(f"{CRM_URL}/api/bot/salud/turno/cancelar").mock(
        return_value=httpx.Response(200, json={"ok": True, "bookingId": "bk_1"})
    )
    ctx.llm.replies = [
        # el modelo se apura: intenta cancelar sin que el paciente lo haya dicho
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="cancelar_turno",
                    arguments={"confirmado": True, "frase_del_paciente": ""},
                )
            ],
        ),
        LlmReply(content="¿Querés que cancele tu turno del jueves 10 a las 12:00?"),
    ]
    await despachar(client, ctx, text="no sé si voy a poder ir", esperar=True)
    await asyncio.sleep(0.1)

    assert cancelar.call_count == 0  # la guarda frenó la cancelación
    assert any("¿Querés que cancele" in t for t in _enviados(routes))


async def test_e2e_crm_de_salud_caido_el_turno_sigue_y_no_manda_texto_roto(
    ctx, client, respx_mock
):
    routes = _rutas(respx_mock)
    respx_mock.post(f"{CRM_URL}/api/bot/salud/urgencia").mock(
        return_value=httpx.Response(503, json={})
    )
    ctx.llm.replies = [
        LlmReply(
            content=None,
            tool_calls=[
                ToolCall(id="t1", name="urgencia", arguments={"nivel": "urgente", "motivo": "m"})
            ],
        ),
        LlmReply(content="Voy a pasarte con una persona del equipo ahora mismo."),
    ]
    resp = await despachar(client, ctx, text="me desmayé hace un rato", esperar=True)
    assert resp.status_code == 200
    await asyncio.sleep(0.1)
    enviados = _enviados(routes)
    assert enviados and all("Traceback" not in t and "crm_error" not in t for t in enviados)


async def test_sena_sin_como_pagar_no_inventa_alias(rt, respx_mock):
    runtime, ctx, conv = rt
    await ctx.store.replace_offered_slots(
        conv.id,
        [OfferedSlot(conversation_id=conv.id, start_utc=SLOT_DT, end_utc=None,
                     label="jueves 10, 12:00", professional_id="pro_a", professional_name="Dra. Pérez")],
    )
    # El catálogo no responde: la cita YA existe y la confirmación no se cae.
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(return_value=httpx.Response(503, json={}))
    respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={"bookingId": "bk_1", "label": "jueves 10, 12:00", "linkPending": False,
                  "depositAmount": 3000, "depositStatus": "pendiente"},
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(return_value=httpx.Response(200, json={}))
    r = await runtime.execute(
        "book_session", {"start_utc": SLOT_ISO, "dia_confirmado": "sí", "sin_obra_social": "particular"}
    )
    assert r["ok"] is True
    final = runtime.finalize_reply("texto del modelo que se descarta")
    assert "seña de $3000" in final
    assert "Una persona del equipo te pasa los datos" in final


async def test_guardar_paciente_resuelve_obra_social_por_nombre(rt_sin_datos, respx_mock):
    runtime, ctx, conv = rt_sin_datos
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(
        return_value=httpx.Response(200, json={"insurances": [{"id": "ins_9", "name": "OSDE", "notes": None}]})
    )
    ruta = respx_mock.put(f"{CRM_URL}/api/bot/salud/paciente").mock(return_value=httpx.Response(200, json={}))
    r = await runtime.execute("guardar_paciente", {"documento": "34244083", "obra_social": "Osde"})
    assert r["ok"] is True
    cuerpo = json.loads(ruta.calls[0].request.content)
    assert cuerpo["insuranceId"] == "ins_9" and cuerpo["documentNumber"] == "34244083"


async def test_obra_social_no_cargada_no_se_inventa_y_no_reserva(rt_sin_datos, respx_mock):
    runtime, ctx, conv = rt_sin_datos
    await _ofrecer(ctx, conv)
    respx_mock.get(f"{CRM_URL}/api/bot/salud/catalog").mock(
        return_value=httpx.Response(200, json={"insurances": []})
    )
    ruta = respx_mock.put(f"{CRM_URL}/api/bot/salud/paciente").mock(return_value=httpx.Response(200, json={}))
    reserva = respx_mock.post(f"{CRM_URL}/api/bot/bookings")
    r = await runtime.execute("guardar_paciente", {"documento": "34244083", "obra_social": "Osde"})
    assert r["ok"] is False and r["error"] == "obra_social_no_cargada"
    # el documento sí quedó guardado
    assert json.loads(ruta.calls[0].request.content) == {"conversationId": CRM_CONV_ID, "documentNumber": "34244083"}
    b = await runtime.execute("book_session", {"start_utc": SLOT_ISO, "dia_confirmado": "sí"})
    assert b["ok"] is False and not reserva.called


async def test_sin_obra_social_solo_vale_si_dice_particular(rt, respx_mock):
    runtime, ctx, conv = rt
    await _ofrecer(ctx, conv)
    reserva = respx_mock.post(f"{CRM_URL}/api/bot/bookings")
    r = await runtime.execute(
        "book_session", {"start_utc": SLOT_ISO, "dia_confirmado": "sí", "sin_obra_social": "tengo Osde"}
    )
    assert r["ok"] is False and not reserva.called
