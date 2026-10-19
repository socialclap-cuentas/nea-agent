"""Markdown del LLM → formato de WhatsApp.

WhatsApp no interpreta `**negrita**`, `[texto](url)` ni `# títulos`: llegan como
símbolos sueltos. Esta red de seguridad se aplica a TODO texto saliente, aunque
el prompt ya pida el formato correcto.
"""
from __future__ import annotations

import re

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def to_whatsapp(text: str) -> str:
    def link(m: re.Match[str]) -> str:
        label, url = m.group(1).strip(), m.group(2)
        return url if label.rstrip("/") == url.rstrip("/") else f"{label}: {url}"

    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"*{m.group(1) or m.group(2)}*", text)
    text = _HEADING.sub(lambda m: f"*{m.group(1)}*", text)
    return text
