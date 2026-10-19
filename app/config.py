"""Configuración tipada del bot (pydantic-settings).

Todas las variables se documentan en `.env.example`. Los defaults permiten
importar el módulo sin entorno (los tests inyectan valores explícitos);
la validación de lo obligatorio ocurre al arranque real.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def canonical_identity(wa_id: str) -> str:
    """Canonicaliza una identidad de WhatsApp para comparaciones.

    México: Meta a veces reporta `521XXXXXXXXXX` (13 dígitos con el "1" de
    móvil) y a veces `52XXXXXXXXXX` — son la misma persona. Los BSUID y otros
    identificadores pasan tal cual.
    """
    s = wa_id.strip()
    if s.startswith("521") and len(s) == 13 and s.isdigit():
        return "52" + s[3:]
    return s


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Webhook de Meta
    verify_token: str = ""
    meta_app_secret: str = ""  # vacío = no se verifica la firma (dev)
    # Secret PROPIO de la app de Instagram (Instagram business login), distinto
    # del meta_app_secret de la app principal — Meta firma cada canal con el
    # secret de su propia app. Vacío = solo se acepta meta_app_secret.
    instagram_app_secret: str = ""

    # CRM (vocero-crm, bot gateway /api/bot/*)
    crm_base_url: str = "http://localhost:3000"
    crm_webhook_url: str = ""  # incluye el segmento del verify token del CRM
    crm_bot_api_key: str = ""

    # Perfil del negocio (capa de persona; ver app/profile.py)
    agent_name: str = "Nea"  # se usa si el CRM no define nombre
    agent_timezone: str = "America/Mexico_City"  # IANA; fechas del prompt
    # Ventana horaria (hora local del agente) para mandar el seguimiento
    # automático — evita que caiga de madrugada/noche y moleste al lead.
    followup_window_start_hour: int = 9
    followup_window_end_hour: int = 21
    brief_path: str = ""  # markdown local, fallback si el CRM no tiene perfil

    # Modo del agente: "agendamiento" (califica y agenda citas) o "ventas"
    # (asesora y vende con el catálogo de Tiendanube; sin agenda).
    agent_mode: str = "agendamiento"

    # Catálogo de Tiendanube (solo lectura). Ambos vacíos = sin herramientas de
    # catálogo. Con valores, Nea consulta precio y stock EN VIVO.
    tiendanube_store_id: str = ""
    tiendanube_access_token: str = ""
    tiendanube_user_agent: str = "Nea eBags (socialclap.cuentas@gmail.com)"

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Modelo de emergencia: si el principal (openai_model) agota reintentos,
    # se prueba UNA vez más con este antes de rendirse y activar el handoff.
    # Vacío = sin respaldo (comportamiento anterior). gpt-4o-mini es buena
    # opción de respaldo: estable, sin parámetros especiales, distinto
    # proveedor de fallas que los modelos "reasoning" (gpt-5.x).
    openai_fallback_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "whisper-1"  # notas de voz → texto
    history_window: int = 10

    # Guardarraíles y tiempos
    allowed_wa_ids: str = ""  # CSV; vacía = responde a todos (Constitución V)
    coalesce_seconds: float = 4.0
    followup_hours: float = 4.0
    # "Escribiendo…" casi inmediato al recibir un mensaje (antes del coalesce).
    typing_delay_seconds: float = 0.5

    # Pausa y operación
    # Interruptor duro por entorno: true = el bot NO responde (ni seguimientos
    # ni reintentos), pase lo que pase en el panel. El panel /admin maneja la
    # pausa día a día; esto es el freno de emergencia a nivel despliegue.
    agent_paused: bool = False
    # Contraseña del panel /admin (usuario "admin"). Vacío = panel deshabilitado.
    admin_token: str = ""
    # Una respuesta que no pudo entregarse en este tiempo se DESCARTA (ya está
    # vieja: el cliente probablemente siguió escribiendo). 0 = nunca vence.
    pending_send_max_age_minutes: int = 20
    # Etapas del pipeline del CRM a las que el agente puede mover un lead al
    # derivarlo a una persona (CSV; nombres EXACTOS, deben existir en el CRM).
    handoff_stages: str = "Reclamo,Seguimiento de pedido,Presupuesto"

    # Infra
    database_url: str = ""
    port: int = 8000

    # Desarrollo: loguear el JSON crudo de mensajes no-texto entrantes para
    # capturar los formatos reales de Meta (spec 002). Apagar al terminar.
    capture_payloads: bool = False

    @property
    def handoff_stage_names(self) -> list[str]:
        return [p.strip() for p in self.handoff_stages.split(",") if p.strip()]

    @property
    def allowed_identities(self) -> frozenset[str]:
        """Allowlist canonicalizada; vacía = sin restricción."""
        return frozenset(
            canonical_identity(part)
            for part in self.allowed_wa_ids.split(",")
            if part.strip()
        )
