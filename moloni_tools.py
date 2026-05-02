import os
import time
import requests
from dotenv import load_dotenv
from typing import Optional, Dict, Any

load_dotenv()

MOLONI_BASE_URL = "https://api.moloni.pt/v1"
COMPANY_ID = int(os.environ["MOLONI_COMPANY_ID"])
DEVELOPER_ID = os.environ["MOLONI_DEVELOPER_ID"]
CLIENT_SECRET = os.environ["MOLONI_CLIENT_SECRET"]
USERNAME = os.environ["MOLONI_USERNAME"]
PASSWORD = os.environ["MOLONI_PASSWORD"]
DRY_RUN = os.getenv("MOLONI_DRY_RUN", "true").lower() == "true"

_token_cache = {
    "access_token": None,
    "expires_at": 0,
}


def moloni_connect() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    url = (
        f"{MOLONI_BASE_URL}/grant/"
        f"?grant_type=password"
        f"&client_id={DEVELOPER_ID}"
        f"&client_secret={CLIENT_SECRET}"
        f"&username={USERNAME}"
        f"&password={PASSWORD}"
    )

    response = requests.get(url, timeout=30)
    response.raise_for_status()
    data = response.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data["expires_in"]) - 60

    return _token_cache["access_token"]


def moloni_post(endpoint: str, body: Dict[str, Any]) -> Dict[str, Any] | list:
    if DRY_RUN and endpoint.endswith("insert"):
        return {
            "dry_run": True,
            "endpoint": endpoint,
            "body": body,
        }

    access_token = moloni_connect()
    url = f"{MOLONI_BASE_URL}/{endpoint}/?access_token={access_token}&json=true"

    print("MOLONI ENDPOINT:", endpoint, flush=True)
    print("MOLONI REQUEST BODY:", body, flush=True)

    response = requests.post(url, json=body, timeout=30)

    print("MOLONI STATUS:", response.status_code, flush=True)
    print("MOLONI RESPONSE:", response.text, flush=True)

    if response.status_code != 200:
        raise RuntimeError(f"Moloni API error {response.status_code}: {response.text}")

    return response.json()


def product_by_reference(reference: str) -> Optional[Dict[str, Any]]:
    body = {
        "company_id": COMPANY_ID,
        "reference": reference,
        "exact": 1,
        "qty": 1,
        "offset": 0,
    }

    data = moloni_post("products/getByReference", body)

    if not data:
        return None

    return data[0]


def create_product(product: Dict[str, Any]) -> Dict[str, Any]:
    existing = product_by_reference(product["reference"])

    if existing:
        return {
            "status": "exists",
            "product": existing,
        }

    body = {
        "company_id": COMPANY_ID,
        "category_id": product["category_id"],
        "type": 1,
        "name": product["name"],
        "summary": product.get("summary", ""),
        "reference": product["reference"],
        "ean": product.get("ean", ""),
        "price": product["price"],
        "unit_id": product["unit_id"],
        "has_stock": 1,
        "stock": 0,
        "minimum_stock": 0,
        "pos_favorite": 0,
        "at_product_category": "M",
        "exemption_reason": "",
        "taxes": product["taxes"],
        "suppliers": product.get("suppliers", []),
        "properties": product.get("properties", []),
    }

    created = moloni_post("products/insert", body)

    return {
        "status": "created",
        "product": created,
    }


def create_supplier_invoice(invoice: Dict[str, Any]) -> Dict[str, Any]:
    body = {
        "company_id": COMPANY_ID,
        "date": invoice["date"],
        "expiration_date": invoice["expiration_date"],
        "maturity_date_id": invoice["maturity_date_id"],
        "document_set_id": invoice["document_set_id"],
        "supplier_id": invoice["supplier_id"],
        "your_reference": invoice.get("your_reference", ""),
        "financial_discount": invoice.get("financial_discount", 0),
        "special_discount": invoice.get("special_discount", 0),
        "related_documents_notes": invoice.get("related_documents_notes", ""),
        "products": invoice["products"],
        "delivery_method_id": invoice["delivery_method_id"],
        "delivery_datetime": invoice["delivery_datetime"],
        "status": invoice.get("status", 0),
    }
    created = moloni_post("supplierInvoices/insert", body)

    return {
        "status": "created",
        "invoice": created,
    }


def get_suppliers() -> list:
    body = {"company_id": COMPANY_ID}
    return moloni_post("suppliers/getAll", body)


def get_product_categories(parent_id: int = 0) -> list:
    body = {"company_id": COMPANY_ID}
    if parent_id:
        body["parent_id"] = parent_id
    return moloni_post("productCategories/getAll", body)


def create_product_category(name: str, parent_id: int = 0) -> dict:
    body = {
        "company_id": COMPANY_ID,
        "parent_id": parent_id,
        "name": name,
        "description": "",
        "pos_enabled": 1,
    }

    return moloni_post("productCategories/insert", body)


def get_products_by_category(category_id: int) -> list:
    PAGE_SIZE = 50
    MAX_PAGES = 200
    results = []
    offset = 0
    for _ in range(MAX_PAGES):
        body = {
            "company_id": COMPANY_ID,
            "category_id": category_id,
            "qty": PAGE_SIZE,
            "offset": offset,
        }
        page = moloni_post("products/getAll", body)
        if not isinstance(page, list):
            break
        results.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return results


