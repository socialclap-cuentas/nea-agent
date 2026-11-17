"""gpt-5.x en OpenAI: rechaza `reasoning` y, con herramientas, exige
`reasoning_effort="none"`. Nea se acomoda sola y contesta en el mismo turno."""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm import OpenAiLlm

URL = "https://api.openai.com/v1/chat/completions"
TOOLS = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object", "properties": {}}}}]


def _err(msg: str, param: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={"error": {"message": msg, "type": "invalid_request_error", "param": param, "code": None}},
    )


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": "c", "object": "chat.completion", "created": 1, "model": "m",
              "choices": [{"index": 0, "finish_reason": "stop",
                           "message": {"role": "assistant", "content": "Hola"}}]},
    )


@pytest.fixture
def sin_espera(monkeypatch):
    async def _no(_): return None
    monkeypatch.setattr("app.llm.asyncio.sleep", _no)


async def test_se_acomoda_a_reasoning_none_y_contesta(respx_mock, sin_espera):
    ruta = respx_mock.post(URL).mock(side_effect=[
        _err("Unknown parameter: 'reasoning'.", "reasoning"),
        _err("Function tools with reasoning_effort are not supported for m in /v1/chat/completions. "
             "To use function tools, use /v1/responses or set reasoning_effort to 'none'.", "reasoning_effort"),
        _ok(),
    ])
    llm = OpenAiLlm("k", "m", reasoning_effort="minimal")
    r = await llm.complete([{"role": "user", "content": "hola"}], tools=TOOLS)
    assert r.content == "Hola"
    ultimo = json.loads(ruta.calls[-1].request.content)
    assert ultimo["reasoning_effort"] == "none"
    assert "reasoning" not in ultimo


async def test_no_manda_reasoning_effort_si_nadie_lo_pidio(respx_mock):
    ruta = respx_mock.post(URL).mock(return_value=_ok())
    llm = OpenAiLlm("k", "m")
    await llm.complete([{"role": "user", "content": "hola"}], tools=TOOLS)
    assert "reasoning_effort" not in json.loads(ruta.calls[0].request.content)
