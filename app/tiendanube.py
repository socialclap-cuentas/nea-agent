"""Cliente de solo-lectura del catálogo de Tiendanube (API v1).

Nea consulta el catálogo EN VIVO (precio y stock siempre al día) y le entrega
al LLM una versión compacta: nada de HTML, ids internos solo donde la
herramienta los necesita para `ver_producto`. La fuente de verdad de precios y
stock es siempre la tienda — el LLM no puede afirmar nada que no venga de aquí.

Auth: header `Authentication: bearer <token>` + `User-Agent` obligatorio
(https://tiendanube.github.io/api-documentation/intro). Errores de red/HTTP →
`TiendanubeError`; un fallo jamás tumba el turno (tools.py lo traduce a
`{"ok": false}` para el LLM).
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("nea.tiendanube")

API_BASE = "https://api.tiendanube.com/v1"
PRODUCT_FIELDS = "id,name,description,canonical_url,variants,free_shipping,tags"
ORDER_FIELDS = (
    "id,number,status,payment_status,shipping_status,shipping_option,"
    "shipping_tracking_number,shipping_tracking_url,created_at,"
    "contact_phone,contact_email,products"
)
CACHE_TTL_SECONDS = 60.0
MAX_RESULTS = 6
DESCRIPTION_CHARS = 350

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class TiendanubeError(Exception):
    """Falla al consultar Tiendanube (red, auth, rate limit, respuesta rara)."""


def _loc(value: Any) -> str:
    """Los textos de Tiendanube vienen localizados: {"es": "...", "pt": "..."}."""
    if isinstance(value, dict):
        for key in ("es", "es_AR", "es_MX", "pt", "en"):
            if value.get(key):
                return str(value[key])
        for v in value.values():
            if v:
                return str(v)
        return ""
    return "" if value is None else str(value)


def _plain(value: Any, limit: int | None = None) -> str:
    text = html.unescape(_TAG_RE.sub(" ", _loc(value)))
    text = _WS_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_ars(amount: Any) -> str | None:
    """32161.09 → '$32.161,09' (formato argentino)."""
    number = _to_float(amount)
    if number is None:
        return None
    entero, _, dec = f"{number:,.2f}".partition(".")
    return f"${entero.replace(',', '.')},{dec}"


def _variant(raw: dict[str, Any]) -> dict[str, Any]:
    values = raw.get("values") or []
    option = " / ".join(filter(None, (_plain(v) for v in values)))
    price = _to_float(raw.get("price"))
    promo = _to_float(raw.get("promotional_price"))
    stock = raw.get("stock")  # None = stock ilimitado / sin control
    manages_stock = raw.get("stock_management", stock is not None)
    if manages_stock and isinstance(stock, (int, float)):
        en_stock = stock > 0
        unidades: int | None = int(stock)
    else:
        en_stock = True
        unidades = None
    out: dict[str, Any] = {
        "opcion": option or "única",
        "precio": format_ars(price),
        "en_stock": en_stock,
    }
    if promo is not None and price is not None and promo < price:
        out["precio_promocional"] = format_ars(promo)
    if unidades is not None:
        out["unidades_disponibles"] = unidades
    if raw.get("sku"):
        out["sku"] = str(raw["sku"])
    return out


def normalize_product(raw: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    variants = [_variant(v) for v in (raw.get("variants") or []) if isinstance(v, dict)]
    out: dict[str, Any] = {
        "producto_id": str(raw.get("id")),
        "nombre": _plain(raw.get("name")),
        "url": raw.get("canonical_url") or raw.get("product_url") or "",
        "variantes": variants,
        "envio_gratis": bool(raw.get("free_shipping")),
        "disponible": any(v["en_stock"] for v in variants) if variants else False,
    }
    desc = _plain(raw.get("description"), None if detail else DESCRIPTION_CHARS)
    if desc:
        out["descripcion"] = desc
    if detail and raw.get("tags"):
        out["etiquetas"] = [str(t) for t in raw["tags"]]
    return out


_ORDER_STATUS = {"open": "abierto", "closed": "cerrado", "cancelled": "cancelado"}
_PAYMENT_STATUS = {
    "pending": "pago pendiente",
    "authorized": "pago autorizado, a la espera de acreditarse",
    "paid": "pagado",
    "partially_paid": "pagado parcialmente",
    "voided": "pago anulado (no se completó)",
    "expired": "pago vencido",
    "refunded": "reintegrado",
    "partially_refunded": "reintegrado parcialmente",
    "abandoned": "compra abandonada (no se completó el pago)",
    "chargeback": "contracargo",
}
_SHIPPING_STATUS = {
    "unpacked": "todavía sin preparar",
    "unfulfilled": "todavía sin preparar",
    "in_preparation": "en preparación",
    "partially_packed": "en preparación",
    "packed": "empaquetado, listo para despachar",
    "unshipped": "todavía no despachado",
    "ready_for_pickup": "listo para retirar",
    "pickup_ready": "listo para retirar",
    "shipped": "despachado, en camino",
    "dispatched": "despachado, en camino",
    "partially_fulfilled": "enviado parcialmente",
    "fulfilled": "enviado",
    "delivered": "entregado",
    "marked_as_fulfilled": "enviado",
}


def _es(table: dict[str, str], value: Any) -> str | None:
    if not value:
        return None
    return table.get(str(value), str(value))


def normalize_order(raw: dict[str, Any]) -> dict[str, Any]:
    """Pedido compacto para el LLM. Sin dirección, nombre, email ni teléfono.

    `_phone` / `_email` viajan SOLO para verificar identidad en tools.py, que los
    quita antes de entregarle algo al LLM.
    """
    tracking = str(raw.get("shipping_tracking_number") or "").strip()
    tracking_url = str(raw.get("shipping_tracking_url") or "").strip()
    out: dict[str, Any] = {
        "numero_pedido": raw.get("number"),
        "estado": _es(_ORDER_STATUS, raw.get("status")),
        "pago": _es(_PAYMENT_STATUS, raw.get("payment_status")),
        "envio": _es(_SHIPPING_STATUS, raw.get("shipping_status")),
        "metodo_envio": _plain(raw.get("shipping_option")) or None,
        "fecha": str(raw.get("created_at") or "")[:10] or None,
        "productos": [
            {"nombre": _plain(p.get("name")), "cantidad": p.get("quantity")}
            for p in (raw.get("products") or [])
            if isinstance(p, dict)
        ],
        "numero_seguimiento": tracking or None,
        "link_seguimiento": tracking_url or None,
        "_phone": str(raw.get("contact_phone") or ""),
        "_email": str(raw.get("contact_email") or ""),
    }
    return out


class TiendanubeClient:
    def __init__(
        self,
        store_id: str,
        access_token: str,
        *,
        user_agent: str = "Nea eBags (socialclap.cuentas@gmail.com)",
        base_url: str = API_BASE,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._store_id = store_id
        self._http = httpx.AsyncClient(
            base_url=f"{base_url}/{store_id}",
            headers={
                "Authentication": f"bearer {access_token}",
                "User-Agent": user_agent,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )
        self._cache: dict[str, tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise TiendanubeError(f"red: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            # Tiendanube responde 404 ("Last page is 0") cuando no hay resultados.
            self._cache[key] = (now, None)
            return None
        if resp.status_code in (401, 403):
            raise TiendanubeError(f"auth rechazada ({resp.status_code})")
        if resp.status_code == 429:
            raise TiendanubeError("rate limit de Tiendanube")
        if resp.status_code >= 400:
            raise TiendanubeError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TiendanubeError("respuesta no es JSON") from exc
        self._cache[key] = (now, data)
        return data

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Productos publicados que matchean `query`, compactos para el LLM."""
        limit = max(1, min(int(limit or 5), MAX_RESULTS))
        query = (query or "").strip()
        terms = [query] + (
            # Sin resultados con la frase completa → prueba con la palabra más larga.
            [max(query.split(), key=len)] if len(query.split()) > 1 else []
        )
        for term in terms:
            data = await self._get(
                "/products",
                {
                    "q": term,
                    "published": "true",
                    "per_page": limit,
                    "fields": PRODUCT_FIELDS,
                },
            )
            if isinstance(data, list) and data:
                return [normalize_product(p) for p in data if isinstance(p, dict)]
        return []

    async def get(self, product_id: str) -> dict[str, Any] | None:
        data = await self._get(
            f"/products/{product_id}", {"fields": PRODUCT_FIELDS}
        )
        if not isinstance(data, dict):
            return None
        return normalize_product(data, detail=True)

    async def find_order(self, number: int) -> dict[str, Any] | None:
        """Pedido por número visible (el que ve el cliente), o None si no existe."""
        data = await self._get(
            "/orders",
            {"q": number, "per_page": 10, "fields": ORDER_FIELDS},
        )
        if not isinstance(data, list):
            return None
        for raw in data:
            if isinstance(raw, dict) and str(raw.get("number")) == str(number):
                return normalize_order(raw)
        return None
