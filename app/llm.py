"""Capa LLM: OpenAI chat.completions con tool-calling y extracción tolerante.

Gotchas del brief que se honran aquí:
- `content` vacío con tool_calls es NORMAL (turno solo-herramientas).
- Respuesta vacía de verdad (sin content ni tool_calls) o excepción → reintento
  con backoff (2 reintentos). Agotado → `LlmExhausted` y el turno degrada en
  silencio + handoff error (Constitución IV).
- Los `arguments` de las tools pueden venir malformados: JSON inválido → {}.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

logger = logging.getLogger("nea.llm")


class LlmExhausted(Exception):
    """El LLM falló todos los reintentos — el turno debe degradar en silencio."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class Llm(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply: ...

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str: ...


class OpenAiLlm:
    RETRIES = 2  # además del intento inicial

    def __init__(
        self,
        api_key: str,
        model: str,
        transcribe_model: str = "whisper-1",
        base_url: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        # base_url ≠ None → proveedor OpenAI-compatible (p. ej. OpenRouter,
        # para el bench de modelos del 002). Ojo: la transcripción de audio
        # es una API de OpenAI — con otro proveedor degrada honesta
        # (LlmExhausted → fallback), por eso el bench alterno cubre texto.
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        # Modelo de emergencia: si el principal agota reintentos (falla de
        # API, respuesta vacía, deprecación no vista a tiempo), se intenta
        # UNA vez más acá antes de rendirse y activar el handoff. Evita el
        # silencio total cuando el modelo principal tiene un problema
        # puntual — a costa, temporalmente, de una respuesta con un modelo
        # distinto (avisado en el log para poder revisar después).
        self._fallback_model = fallback_model
        self._transcribe_model = transcribe_model
        # Contadores de uso (para el bench de costos del 002): tokens reales
        # reportados por el proveedor, acumulados por instancia.
        self.usage = {"prompt": 0, "cached": 0, "completion": 0, "llamadas": 0}

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str:
        """Audio → texto (API de transcripción). Vacío/fallo → LlmExhausted."""
        last_error: Exception | None = None
        content_type = (mime or "audio/ogg").split(";")[0].strip()
        for attempt in range(2):
            try:
                resp = await self._client.audio.transcriptions.create(
                    model=self._transcribe_model,
                    file=(filename, data, content_type),
                    language="es",
                )
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    return text
                last_error = ValueError("transcripción vacía")
                logger.warning("transcribe: texto vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning("transcribe: fallo en intento %d: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply:
        try:
            return await self._complete_with(self._model, messages, tools)
        except LlmExhausted as primary_error:
            if not self._fallback_model or self._fallback_model == self._model:
                raise
            logger.warning(
                "llm: %s agotado, probando respaldo %s", self._model, self._fallback_model
            )
            try:
                return await self._complete_with(self._fallback_model, messages, tools)
            except LlmExhausted:
                # Si el respaldo también falla, se reporta el error del
                # modelo principal (es el que hay que revisar primero).
                raise primary_error

    async def _complete_with(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> LlmReply:
        last_error: Exception | None = None
        for attempt in range(self.RETRIES + 1):
            try:
                kwargs: dict[str, Any] = {}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                    # Los modelos "reasoning" (familia gpt-5.x: sol/terra/luna,
                    # y o1/o3/o4) rechazan tool-calling en /v1/chat/completions
                    # salvo que se desactive el razonamiento explícitamente.
                    # Los modelos clásicos (gpt-4o-mini, gpt-4.1-mini, etc.)
                    # hacen lo contrario: RECHAZAN el parámetro si se lo mandás
                    # ("unsupported_parameter"). Por eso es condicional a la
                    # familia del modelo, nunca incondicional.
                    if model.startswith(("gpt-5", "o1", "o3", "o4")):
                        kwargs["reasoning_effort"] = "none"
                resp = await self._client.chat.completions.create(
                    model=model, messages=messages, **kwargs
                )
                u = getattr(resp, "usage", None)
                if u is not None:
                    det = getattr(u, "prompt_tokens_details", None)
                    self.usage["llamadas"] += 1
                    self.usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                    self.usage["completion"] += getattr(u, "completion_tokens", 0) or 0
                    self.usage["cached"] += getattr(det, "cached_tokens", 0) or 0
                reply = self._parse(resp)
                if reply.content or reply.tool_calls:
                    return reply
                # Antes esto solo decía "vacía" sin explicar por qué — ahora
                # capturamos refusal (el modelo se negó, ej. filtro de
                # seguridad) y finish_reason (ej. "length" = se cortó por
                # límite de tokens) para diagnosticar la causa real.
                choice = (getattr(resp, "choices", None) or [None])[0]
                message = getattr(choice, "message", None) if choice else None
                refusal = getattr(message, "refusal", None) if message else None
                finish_reason = getattr(choice, "finish_reason", None) if choice else None
                last_error = ValueError(
                    f"respuesta vacía del LLM ({model}, sin content ni tools) "
                    f"finish_reason={finish_reason!r} refusal={refusal!r}"
                )
                logger.warning(
                    "llm: respuesta vacía (%s), intento %d — finish_reason=%s refusal=%s",
                    model, attempt + 1, finish_reason, refusal,
                )
            except Exception as exc:  # red, API, parseo — todo reintenta
                last_error = exc
                logger.warning("llm: fallo en intento %d (%s): %s", attempt + 1, model, exc)
            if attempt < self.RETRIES:
                await asyncio.sleep(2**attempt)  # 1 s, 2 s
        raise LlmExhausted(str(last_error))

    @staticmethod
    def _parse(resp: Any) -> LlmReply:
        """Extracción tolerante: nunca truena por formato inesperado."""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return LlmReply(content=None)
        message = getattr(choices[0], "message", None)
        if message is None:
            return LlmReply(content=None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            content = content.strip() or None
        else:
            content = None
        tool_calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            raw_args = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {}
            except (TypeError, ValueError):
                logger.warning("llm: arguments malformados en %s — uso {}", name)
                args = {}
            tool_calls.append(
                ToolCall(id=getattr(tc, "id", "") or "", name=name, arguments=args)
            )
        return LlmReply(content=content, tool_calls=tool_calls)
