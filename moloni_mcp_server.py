import asyncio
import json
import os
import time
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
_CACHE_TTL = 60.0
_CACHE_MAX = 256


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
async def list_products_by_category(category_id: int) -> list[dict]:
    """List every product under a Moloni category in a single call.
    Use this to check existence of multiple product references against one
    parent category. Much faster than calling search_product_by_reference
    multiple times — only one Moloni API call is needed per category.
    Returns a list of objects: [{"product_id": int, "reference": str,
    "name": str, "price": float, "category_id": int, "ean": str}, ...].
    Empty list if the category has no products.
    """
    raw = await asyncio.to_thread(get_products_by_category, category_id)
    return [
        {
            "product_id": p.get("product_id") or 0,
            "reference": p.get("reference") or "",
            "name": p.get("name") or "",
            "price": float(p.get("price") or 0),
            "category_id": p.get("category_id") or 0,
            "ean": p.get("ean") or "",
        }
        for p in (raw if isinstance(raw, list) else [])
    ]


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
