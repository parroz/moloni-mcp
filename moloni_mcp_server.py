import asyncio
import json
import os
import time
from collections import deque
import uvicorn
from mcp.server.fastmcp import FastMCP

from moloni_tools import (
    product_by_reference,
    create_product,
    create_supplier_invoice,
    get_suppliers,
    get_product_categories,
    get_products_by_category,
    create_product_category,
)


MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN")

mcp = FastMCP("moloni")

# Reference lookup cache: {reference: (monotonic_timestamp, product_or_None)}
_ref_cache: dict[str, tuple[float, dict | None]] = {}
_category_products_cache: dict[tuple[int, bool], tuple[float, list[dict]]] = {}
_CACHE_TTL = 60.0
_CACHE_MAX = 256

_MAX_CATEGORY_VISITS = 100
_MAX_PRODUCTS_COLLECTED = 10_000
_CATEGORY_LIST_CONCURRENCY = 6
_CATEGORY_LIST_TIMEOUT_FLAT = 60.0
_CATEGORY_LIST_TIMEOUT_RECURSIVE = 180.0


class BearerAuthMiddleware:
    """ASGI middleware that validates Authorization: Bearer <MCP_AUTH_TOKEN>."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if MCP_AUTH_TOKEN and scope["type"] in ("http", "websocket"):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if token != MCP_AUTH_TOKEN:
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [[b"content-type", b"text/plain"]],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def normalize_supplier_invoice_products(products):
    normalized = []
    for p in products:
        normalized.append({
            "product_id": p["product_id"],
            "name": p["name"],
            "summary": p.get("summary", p["name"]),
            "qty": p["qty"],
            "price": p["price"],
            "discount": p.get("discount", 0),
            "deduction_id": p.get("deduction_id", 0),
            "order": p.get("order", 0),
            "exemption_reason": p.get("exemption_reason", "M10"),
            "warehouse_id": p.get("warehouse_id", 0),
            "taxes": p.get("taxes", []),
        })
    return normalized


def _normalize_product_rows(raw: list) -> list[dict]:
    return [
        {
            "product_id": p.get("product_id") or 0,
            "reference": p.get("reference") or "",
            "name": p.get("name") or "",
            "price": float(p.get("price") or 0),
            "category_id": p.get("category_id") or 0,
            "ean": p.get("ean") or "",
        }
        for p in raw
        if isinstance(p, dict)
    ]


def _child_category_ids_from_response(rows) -> list[int]:
    if not isinstance(rows, list):
        return []
    out: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("category_id")
        if cid is None:
            continue
        try:
            out.append(int(cid))
        except (TypeError, ValueError):
            continue
    return out


def _dedupe_raw_products_by_id(products: list) -> list:
    seen: set[int] = set()
    out = []
    for p in products:
        if not isinstance(p, dict):
            continue
        pid = p.get("product_id")
        if pid is not None:
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                pid_i = None
        else:
            pid_i = None
        if pid_i is not None:
            if pid_i in seen:
                continue
            seen.add(pid_i)
        out.append(p)
    return out


async def _list_products_by_category_uncached(category_id: int, recursive: bool) -> list[dict]:
    if not recursive:
        raw = await asyncio.to_thread(get_products_by_category, category_id)
        return _normalize_product_rows(raw if isinstance(raw, list) else [])

    queue: deque[int] = deque([category_id])
    processed: set[int] = set()
    accum: list = []
    sem = asyncio.Semaphore(_CATEGORY_LIST_CONCURRENCY)

    async def work(cid: int):
        async with sem:
            children = await asyncio.to_thread(get_product_categories, cid)
        async with sem:
            products = await asyncio.to_thread(get_products_by_category, cid)
        return children, products

    while (
        queue
        and len(processed) < _MAX_CATEGORY_VISITS
        and len(accum) < _MAX_PRODUCTS_COLLECTED
    ):
        batch: list[int] = []
        while (
            queue
            and len(batch) < _CATEGORY_LIST_CONCURRENCY
            and len(processed) + len(batch) < _MAX_CATEGORY_VISITS
        ):
            cid = queue.popleft()
            if cid in processed:
                continue
            batch.append(cid)
        if not batch:
            break
        outcomes = await asyncio.gather(*(work(cid) for cid in batch))
        for cid, (children_raw, products_raw) in zip(batch, outcomes):
            processed.add(cid)
            if isinstance(products_raw, list):
                for p in products_raw:
                    if len(accum) >= _MAX_PRODUCTS_COLLECTED:
                        break
                    accum.append(p)
            for child_id in _child_category_ids_from_response(children_raw):
                queue.append(child_id)
            if len(accum) >= _MAX_PRODUCTS_COLLECTED:
                break

    deduped = _dedupe_raw_products_by_id(accum)
    return _normalize_product_rows(deduped)


@mcp.tool()
def ping() -> str:
    """Simple health check. Returns 'pong'."""
    return "pong"


@mcp.tool()
async def search_product_by_reference(reference: str) -> dict:
    """
    Search Moloni by exact product reference (case-insensitive).
    Always call this BEFORE create_product_in_moloni.
    Returns {found, product_id, name, price, category_id} on match, {found: false} on miss.
    Returns {found: false, error: "moloni_timeout"} if Moloni does not respond within 15 s.
    Results are cached for 60 s — safe to call multiple times for the same reference.
    Prefer list_products_by_category when checking many references under the same category.
    """
    now = time.monotonic()
    cached = _ref_cache.get(reference)
    if cached and now - cached[0] < _CACHE_TTL:
        product = cached[1]
        if product is None:
            return {"found": False}
        return {
            "found": True,
            "product_id": product.get("product_id"),
            "name": product.get("name"),
            "price": product.get("price"),
            "category_id": product.get("category_id"),
        }

    try:
        product = await asyncio.wait_for(
            asyncio.to_thread(product_by_reference, reference),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return {"found": False, "error": "moloni_timeout"}

    if len(_ref_cache) >= _CACHE_MAX:
        _ref_cache.clear()
    _ref_cache[reference] = (time.monotonic(), product)

    if not product:
        return {"found": False}
    return {
        "found": True,
        "product_id": product.get("product_id"),
        "name": product.get("name"),
        "price": product.get("price"),
        "category_id": product.get("category_id"),
    }


@mcp.tool()
def create_product_in_moloni(
    reference: str,
    name: str,
    price: float,
    category_id: int,
    unit_id: int,
    tax_id: int,
    tax_value: float,
    summary: str = "",
    ean: str = "",
    supplier_id: int = 0,
    supplier_reference: str = "",
    cost_price: float = 0,
    approved: bool = True,
) -> dict:
    """
    Create a product in Moloni. Idempotent: if a product with this reference already exists,
    returns the existing product_id without creating a duplicate.
    Always call search_product_by_reference first. approved is accepted but ignored.
    """
    existing = product_by_reference(reference)
    if existing:
        return {
            "created": False,
            "status": "exists",
            "product_id": existing.get("product_id"),
            "name": existing.get("name"),
        }

    suppliers = []
    if supplier_id:
        suppliers.append({
            "supplier_id": supplier_id,
            "cost_price": cost_price,
            "reference": supplier_reference or reference,
        })

    product = {
        "reference": reference,
        "name": name,
        "summary": summary,
        "ean": ean,
        "price": price,
        "category_id": category_id,
        "unit_id": unit_id,
        "taxes": [
            {
                "tax_id": tax_id,
                "value": tax_value,
                "order": 1,
                "cumulative": 0,
            }
        ],
        "suppliers": suppliers,
        "properties": [],
    }

    result = create_product(product)
    raw = result.get("product", {}) if isinstance(result, dict) else {}

    return {
        "created": result.get("status") == "created",
        "status": result.get("status"),
        "product_id": raw.get("product_id"),
        "reference": reference,
    }


@mcp.tool()
def list_suppliers() -> list:
    """List all Moloni suppliers. Useful for resolving supplier IDs."""
    return get_suppliers()


@mcp.tool()
def list_product_categories(parent_id: int = 0) -> list:
    """
    List Moloni product categories. Pass parent_id to filter to subcategories
    under a specific parent (e.g. parent_id=6549313 for American Vintage).
    Returns all categories when parent_id=0.
    """
    return get_product_categories(parent_id=parent_id)


@mcp.tool()
async def list_products_by_category(category_id: int, recursive: bool = True) -> list[dict]:
    """List every product under a Moloni category, INCLUDING subcategories.
    Pass a parent category and you get back every product anywhere in its
    tree in one call. Use this to check existence of multiple product
    references against a known parent — much faster than calling
    search_product_by_reference repeatedly.
    Set recursive=False to limit to products filed directly in this exact
    category and ignore subcategories.
    Returns: [{"product_id": int, "reference": str, "name": str,
    "price": float, "category_id": int, "ean": str}, ...]. Empty if none.
    """
    now = time.monotonic()
    cache_key = (category_id, recursive)
    cached = _category_products_cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    timeout = (
        _CATEGORY_LIST_TIMEOUT_RECURSIVE if recursive else _CATEGORY_LIST_TIMEOUT_FLAT
    )
    try:
        result = await asyncio.wait_for(
            _list_products_by_category_uncached(category_id, recursive),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return []

    if len(_category_products_cache) >= _CACHE_MAX:
        _category_products_cache.clear()
    _category_products_cache[cache_key] = (time.monotonic(), result)
    return result


@mcp.tool()
def create_category_in_moloni(
    name: str,
    parent_id: int,
    approved: bool = True,
) -> dict:
    """
    Create a Moloni product category under parent_id.
    approved is accepted but ignored.
    """
    return create_product_category(name=name, parent_id=parent_id)


@mcp.tool()
def create_supplier_invoice_in_moloni(
    date: str,
    expiration_date: str,
    maturity_date_id: int,
    document_set_id: int,
    supplier_id: int,
    delivery_method_id: int,
    delivery_datetime: str,
    products_json: str,
    your_reference: str,
    financial_discount: float = 0,
    special_discount: float = 0,
    status: int = 0,
    approved: bool = True,
) -> dict:
    """
    Create a supplier invoice in Moloni.
    products_json must be a JSON string array of invoice line objects.
    approved is accepted but ignored.
    Returns {valid: 1, document_id, your_reference} on success,
    {valid: 0, errors: [...]} on Moloni error.

    Example products_json:
    [{"product_id": 229889326, "name": "SWEAT ZIPPE ML CAPUCHE TURQUOISE S",
      "summary": "American Vintage | BOBY03FE26 | TURQUOISE | S",
      "qty": 1, "price": 55.80, "discount": 0, "deduction_id": 0,
      "order": 0, "exemption_reason": "M10", "warehouse_id": 0,
      "taxes": [{"tax_id": 2537703, "value": 0, "order": 1, "cumulative": 0}]}]
    """
    try:
        products = json.loads(products_json)
    except json.JSONDecodeError as exc:
        return {
            "valid": 0,
            "errors": [f"Invalid products_json: {exc}"],
            "products_json_received": products_json,
        }

    if not isinstance(products, list):
        return {
            "valid": 0,
            "errors": [f"products_json must decode to a list, got {type(products).__name__}"],
        }

    products = normalize_supplier_invoice_products(products)

    invoice = {
        "date": date,
        "expiration_date": expiration_date,
        "maturity_date_id": maturity_date_id,
        "document_set_id": document_set_id,
        "supplier_id": supplier_id,
        "your_reference": your_reference,
        "financial_discount": financial_discount,
        "special_discount": special_discount,
        "delivery_method_id": delivery_method_id,
        "delivery_datetime": delivery_datetime,
        "status": status,
        "products": products,
    }

    print("CREATE SUPPLIER INVOICE TOOL CALLED", flush=True)
    print("INVOICE PAYLOAD:", invoice, flush=True)

    result = create_supplier_invoice(invoice)

    print("MOLONI SUPPLIER INVOICE RESULT:", result, flush=True)

    invoice_response = result.get("invoice") if isinstance(result, dict) else result

    if isinstance(invoice_response, dict) and invoice_response.get("valid") == 1 and invoice_response.get("document_id"):
        return {
            "valid": 1,
            "document_id": invoice_response["document_id"],
            "your_reference": your_reference,
        }

    errors = invoice_response if isinstance(invoice_response, list) else [str(invoice_response)]
    return {
        "valid": 0,
        "errors": errors,
    }


if __name__ == "__main__":
    app = mcp.sse_app()
    app = BearerAuthMiddleware(app)
    uvicorn.run(app, host="0.0.0.0", port=9000)
