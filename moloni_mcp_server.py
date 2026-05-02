import json
from mcp.server.fastmcp import FastMCP

from moloni_tools import (
    product_by_reference,
    create_product,
    create_supplier_invoice,
    get_suppliers,
    get_product_categories,
    create_product_category,
)


mcp = FastMCP("moloni", host="127.0.0.1", port=9000)



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
            "taxes": [],
        })

    return normalized



@mcp.tool()
def ping() -> str:
    """Simple health check tool."""
    return "pong"


@mcp.tool()
def search_product_by_reference(reference: str) -> dict:
    """Search Moloni for a product by exact reference."""
    product = product_by_reference(reference)
    return {
        "found": product is not None,
        "product": product,
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
    approved: bool = False,
) -> dict:
    """
    Create a generic product in Moloni.

    The caller must calculate:
    - reference
    - name
    - price
    - category_id
    - unit_id
    - tax_id
    - tax_value

    Always search by reference first.
    Requires approved=True.
    """
    if not approved:
        return {
            "created": False,
            "reason": "Approval required. Call again with approved=true only after explicit user approval.",
        }

    existing = product_by_reference(reference)
    if existing:
        return {
            "created": False,
            "status": "exists",
            "product": existing,
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

    return {
        "created": result.get("status") == "created",
        "reference": reference,
        "result": result,
    }



@mcp.tool()
def list_suppliers() -> list:
    """List Moloni suppliers."""
    return get_suppliers()


@mcp.tool()
def list_product_categories() -> list:
    """List Moloni product categories."""
    return get_product_categories()


@mcp.tool()
def create_category_in_moloni(
    name: str,
    parent_id: int,
    approved: bool = False,
) -> dict:
    """Create a Moloni product category under a parent category."""
    if not approved:
        return {
            "created": False,
            "reason": "Approval required. Call again with approved=true.",
        }

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
    approved: bool = False,
) -> dict:
    """
    Create a supplier invoice in Moloni.

    Use products_json only. Do not pass products.
    products_json must be a JSON string array of supplier invoice line objects.

    Example products_json:
    [
      {
        "product_id": 229889326,
        "name": "SWEAT ZIPPE ML CAPUCHE TURQUOISE S",
        "summary": "American Vintage | BOBY03FE26 | TURQUOISE | S",
        "qty": 1,
        "price": 55.80,
        "discount": 0,
        "deduction_id": 0,
        "order": 0,
        "exemption_reason": "M10",
        "warehouse_id": 0,
        "taxes": [
          {
            "tax_id": 2537703,
            "value": 0,
            "order": 1,
            "cumulative": 0
          }
        ]
      }
    ]

    Requires approved=True.
    """
    if not approved:
        return {
            "created": False,
            "reason": "Approval required. Call again with approved=true.",
        }

    try:
        products = json.loads(products_json)
    except json.JSONDecodeError as exc:
        return {
            "created": False,
            "error": "Invalid products_json. Must be valid JSON string.",
            "details": str(exc),
            "products_json_received": products_json,
        }

    if not isinstance(products, list):
        return {
            "created": False,
            "error": "products_json must decode to a list of product lines.",
            "decoded_type": type(products).__name__,
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

    if isinstance(invoice_response, list):
        return {
            "status": "failed",
            "created": False,
            "moloni_response": invoice_response,
        }

    if isinstance(invoice_response, dict) and invoice_response.get("valid") == 1 and invoice_response.get("document_id"):
        return {
            "status": "created",
            "created": True,
            "document_id": invoice_response["document_id"],
            "moloni_response": invoice_response,
        }

    return {
        "status": "unknown",
        "created": False,
        "moloni_response": invoice_response,
    }




if __name__ == "__main__":
    mcp.run(transport="sse")

