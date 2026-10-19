"""Herramientas del LLM: update_ficha, propose_slots, book_session, route_out, handoff.

La validación es server-side: `book_session` SOLO acepta slots previamente
ofrecidos (tabla offered_slots, comparación por epoch exacto). Un fallo del
CRM dentro de una tool regresa `{"ok": false, ...}` al LLM — nunca tumba el
turno.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from app.crm import CrmError, SlotTaken
from app.profile import BusinessProfile
from app.state import AppContext, Conversation, OfferedSlot
from app.tiendanube import TiendanubeError

logger = logging.getLogger("nea.tools")

MAX_OFFERED = 3

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "update_ficha",
            "description": (
                "Guarda o actualiza la ficha del lead en el CRM (merge: solo los "
                "campos que mandes). Llámala en cuanto descubras un dato nuevo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rubro": {"type": "string"},
                    "rol": {
                        "type": "string",
                        "description": "dueno | hijo_del_dueno | empleado | otro",
                    },
                    "tamano_aprox": {"type": "string"},
                    "volumen_mensajes_dia": {
                        "type": "string",
                        "description": "Cuántos mensajes/consultas recibe por día aprox., tal como lo diga el lead (ej. '20 a 50', 'unos 10').",
                    },
                    "sistemas": {"type": "string"},
                    "dolor_principal": {"type": "string"},
                    "geo": {"type": "string"},
                    "calificado": {"type": "boolean"},
                    "resultado": {
                        "type": "string",
                        "description": "agendo | dio_diy | handoff | sin_respuesta",
                    },
                    "notas": {"type": "string"},
                    "etapa": {
                        "type": "string",
                        "description": (
                            "Mueve al lead en el pipeline del CRM. Usar solo el "
                            "nombre EXACTO de una de estas etapas: 'Nuevo', "
                            "'En conversación', 'Interesado', 'Cliente', 'Perdido'. "
                            "Omitir si no corresponde mover nada en este turno."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_slots",
            "description": (
                "Consulta la disponibilidad real de la agenda del negocio y te "
                "regresa hasta 3 horarios para ofrecer al lead (con etiqueta en "
                "español). SOLO estos horarios serán reservables después."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_session",
            "description": (
                "Reserva la cita en uno de los horarios previamente ofrecidos. "
                "start_utc debe ser EXACTAMENTE el start_utc de un slot ofrecido "
                "en esta conversación."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del slot elegido, tal cual se ofreció",
                    },
                    "con_videollamada": {
                        "type": "boolean",
                        "description": (
                            "true si el lead prefiere videollamada (se genera "
                            "un link de Google Meet), false si prefiere llamada "
                            "de audio o no lo aclaró. Preguntá su preferencia "
                            "antes de reservar."
                        ),
                    },
                },
                "required": ["start_utc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "route_out",
            "description": (
                "Marca al lead como no calificado (hoy). Después despídete con "
                "honestidad, compartiendo los recursos alternativos del negocio "
                "si existen, puerta abierta."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff",
            "description": (
                "Pasa la conversación a un humano del negocio y pausa la IA. Tu "
                "mensaje de despedida se envía ANTES de la pausa — salvo en el "
                "handoff por hostilidad, donde cierras sobrio sin anunciarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo breve (p.ej. 'pidió humano', 'duda fuera del conocimiento')",
                    }
                },
            },
        },
    },
]

CATALOG_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_productos",
            "description": (
                "Busca en el catálogo REAL de la tienda (precio y stock en vivo). "
                "Úsala SIEMPRE antes de mencionar un producto, precio, medida o "
                "disponibilidad. Probá con palabras clave simples (ej. 'doypack', "
                "'bolsa kraft', 'caja pizza'); si no hay resultados, reintentá con "
                "un sinónimo antes de rendirte."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": "Palabras clave del producto buscado",
                    },
                    "max_resultados": {
                        "type": "integer",
                        "description": "Cuántos productos traer (1 a 6, por defecto 4)",
                    },
                },
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ver_producto",
            "description": (
                "Trae el detalle completo de UN producto (todas sus variantes, "
                "precios, stock y descripción) por su producto_id, tal como "
                "salió de buscar_productos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto_id": {"type": "string"},
                },
                "required": ["producto_id"],
            },
        },
    },
]

_VENTAS_FROM_BASE = ("update_ficha", "handoff")


def tool_schemas(settings: Any, *, catalog: bool = False) -> list[dict[str, Any]]:
    """Herramientas expuestas al LLM según el modo del agente.

    - agendamiento (default): las cinco originales (+ catálogo si hay cliente).
    - ventas: ficha + handoff + catálogo; sin agenda ni route_out.
    """
    if getattr(settings, "agent_mode", "agendamiento") == "ventas":
        base = [
            copy.deepcopy(t)
            for t in TOOL_SCHEMAS
            if t["function"]["name"] in _VENTAS_FROM_BASE
        ]
        stages = list(getattr(settings, "handoff_stage_names", []) or [])
        if stages:
            for t in base:
                if t["function"]["name"] == "handoff":
                    t["function"]["parameters"]["properties"]["etapa"] = {
                        "type": "string",
                        "enum": stages,
                        "description": (
                            "Etapa del pipeline a la que mover al cliente al derivarlo, "
                            "si corresponde: Reclamo (algo llegó mal, roto o incompleto), "
                            "Seguimiento de pedido (pregunta por su envío/número de "
                            "seguimiento), Presupuesto (cotización especial, volumen, "
                            "impresión). Omitir si ninguna aplica."
                        ),
                    }
        return base + CATALOG_TOOL_SCHEMAS
    return TOOL_SCHEMAS + (CATALOG_TOOL_SCHEMAS if catalog else [])


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _slots_from_payload(
    conversation_id: int, raw_slots: list[dict[str, Any]]
) -> list[OfferedSlot]:
    """Convierte slots del CRM ({startUtc,endUtc,label}) a OfferedSlot, tolerante."""
    out: list[OfferedSlot] = []
    for raw in raw_slots[:MAX_OFFERED]:
        start = _parse_utc(str(raw.get("startUtc") or ""))
        if start is None:
            continue
        end = _parse_utc(str(raw.get("endUtc") or "")) if raw.get("endUtc") else None
        out.append(
            OfferedSlot(
                conversation_id=conversation_id,
                start_utc=start,
                end_utc=end,
                label=str(raw.get("label") or _iso_z(start)),
            )
        )
    return out


def _slots_for_llm(slots: list[OfferedSlot]) -> list[dict[str, str]]:
    return [{"start_utc": _iso_z(s.start_utc), "label": s.label} for s in slots]


class ToolRuntime:
    """Ejecuta las tool-calls de UN turno y acumula sus efectos."""

    def __init__(
        self,
        ctx: AppContext,
        conv: Conversation,
        crm_conversation_id: str,
        profile: BusinessProfile | None = None,
    ) -> None:
        self._ctx = ctx
        self._conv = conv
        self._crm_conv_id = crm_conversation_id
        self._profile = profile or BusinessProfile()
        # Efectos observables por turn.py:
        self.handoff_reason: str | None = None  # se ejecuta DESPUÉS de la despedida
        self.handoff_stage: str | None = None  # etapa del pipeline a asignar al derivar
        self.booked = False
        self.routed_out = False
        self.proposed = False

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "update_ficha":
                return await self._update_ficha(args)
            if name == "propose_slots":
                return await self._propose_slots()
            if name == "book_session":
                return await self._book_session(args)
            if name == "route_out":
                return await self._route_out()
            if name == "handoff":
                return self._handoff(args)
            if name == "buscar_productos":
                return await self._buscar_productos(args)
            if name == "ver_producto":
                return await self._ver_producto(args)
            logger.warning("tools: herramienta desconocida %r", name)
            return {"ok": False, "error": f"herramienta desconocida: {name}"}
        except CrmError as exc:
            logger.warning("tools: %s falló contra el CRM: %s", name, exc)
            return {
                "ok": False,
                "error": "crm_error",
                "detalle": "no pude completar la acción; continúa la conversación o haz handoff",
            }

    async def _buscar_productos(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self._ctx.tiendanube
        if client is None:
            return {"ok": False, "error": "catalogo_no_disponible"}
        consulta = str(args.get("consulta") or "").strip()
        if not consulta:
            return {"ok": False, "error": "consulta_vacia"}
        try:
            limit = int(args.get("max_resultados") or 4)
        except (TypeError, ValueError):
            limit = 4
        try:
            productos = await client.search(consulta, limit)
        except TiendanubeError as exc:
            logger.warning("tools: buscar_productos falló: %s", exc)
            return {
                "ok": False,
                "error": "catalogo_no_disponible",
                "detalle": "no pude consultar el catálogo ahora; no inventes datos: ofrecé pasar con una persona del equipo",
            }
        if not productos:
            return {
                "ok": True,
                "productos": [],
                "nota": "sin resultados; probá otra palabra clave o preguntá qué necesita exactamente",
            }
        return {"ok": True, "productos": productos}

    async def _ver_producto(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self._ctx.tiendanube
        if client is None:
            return {"ok": False, "error": "catalogo_no_disponible"}
        pid = str(args.get("producto_id") or "").strip()
        if not pid.isdigit():
            return {"ok": False, "error": "producto_id_invalido"}
        try:
            producto = await client.get(pid)
        except TiendanubeError as exc:
            logger.warning("tools: ver_producto falló: %s", exc)
            return {
                "ok": False,
                "error": "catalogo_no_disponible",
                "detalle": "no pude consultar el catálogo ahora; no inventes datos",
            }
        if producto is None:
            return {"ok": False, "error": "producto_no_encontrado"}
        return {"ok": True, "producto": producto}

    async def _update_ficha(self, args: dict[str, Any]) -> dict[str, Any]:
        # Tolera el drift del LLM: manda lo que haya, el CRM normaliza flojo.
        etapa = args.get("etapa")
        ficha = {k: v for k, v in args.items() if v is not None and k != "etapa"}
        if not ficha and not etapa:
            return {"ok": True, "nota": "sin campos nuevos"}
        await self._ctx.crm.put_ficha(self._crm_conv_id, ficha, stage=etapa)
        return {"ok": True}

    async def _propose_slots(self) -> dict[str, Any]:
        raw = await self._ctx.crm.get_availability(limit=6)
        slots = _slots_from_payload(self._conv.id, raw)
        if not slots:
            return {
                "ok": False,
                "error": "sin_disponibilidad",
                "detalle": "no hay horarios abiertos; ofrece handoff para coordinar directo",
            }
        await self._ctx.store.replace_offered_slots(self._conv.id, slots)
        self.proposed = True
        return {
            "ok": True,
            "slots": _slots_for_llm(slots),
            "instrucciones": "ofrece estos horarios con su etiqueta tal cual; máximo 3",
        }

    async def _book_session(self, args: dict[str, Any]) -> dict[str, Any]:
        wanted = _parse_utc(str(args.get("start_utc") or ""))
        offered = await self._ctx.store.get_offered_slots(self._conv.id)
        if wanted is None:
            return {
                "ok": False,
                "error": "start_utc_invalido",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        # Validación server-side por epoch exacto: solo lo ofrecido es reservable.
        chosen = next(
            (s for s in offered if int(s.start_utc.timestamp()) == int(wanted.timestamp())),
            None,
        )
        if chosen is None:
            logger.info(
                "tools: book_session rechazado — %s no está entre los ofrecidos",
                args.get("start_utc"),
            )
            return {
                "ok": False,
                "error": "slot_no_ofrecido",
                "detalle": "solo puedes reservar un horario que ya ofreciste",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        try:
            result = await self._ctx.crm.create_booking(
                self._crm_conv_id,
                _iso_z(chosen.start_utc),
                con_videollamada=bool(args.get("con_videollamada", False)),
            )
        except SlotTaken as exc:
            # El slot se ocupó entre oferta y elección: alternativas frescas.
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        try:
            await self._ctx.crm.put_ficha(
                self._crm_conv_id, {"calificado": True, "resultado": "agendo"}
            )
        except CrmError as exc:  # best-effort: la cita ya existe
            logger.warning("tools: no pude actualizar ficha tras booking: %s", exc)
        return {
            "ok": True,
            "label": result.get("label") or chosen.label,
            "zoom_url": result.get("zoomJoinUrl"),
            "instrucciones": (
                "confirma día y hora de la cita, comparte el link de la "
                "videollamada si existe y menciona lo que el negocio pida "
                "para llegar preparado"
            ),
        }

    async def _route_out(self) -> dict[str, Any]:
        # "dio_diy" es el valor del enum `resultado` en el gateway del CRM
        # (006); el nombre de la herramienta es genérico, el cable no cambia.
        await self._ctx.crm.put_ficha(
            self._crm_conv_id, {"calificado": False, "resultado": "dio_diy"}
        )
        self.routed_out = True
        out: dict[str, Any] = {"ok": True}
        if self._profile.resources:
            out["recursos"] = self._profile.resources
            out["instrucciones"] = "comparte estos recursos al despedirte, puerta abierta"
        return out

    def _handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        self.handoff_reason = str(args.get("reason") or "lead_request")
        etapa = str(args.get("etapa") or "").strip()
        # Solo etapas autorizadas por configuración: el LLM no inventa etapas.
        if etapa and etapa in self._ctx.settings.handoff_stage_names:
            self.handoff_stage = etapa
        return {
            "ok": True,
            "nota": (
                "el pase a humano se ejecutará después de tu mensaje de despedida"
            ),
        }
