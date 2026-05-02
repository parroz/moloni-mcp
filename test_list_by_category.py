"""Manual smoke test: list_products_by_category against live Moloni."""
import json
from moloni_tools import get_products_by_category

AMERICAN_VINTAGE_CATEGORY_ID = 6549313

products = get_products_by_category(AMERICAN_VINTAGE_CATEGORY_ID)

print(f"Total products returned: {len(products)}")
print("\nFirst 3 products:")
for p in products[:3]:
    print(json.dumps({
        "product_id": p.get("product_id"),
        "reference": p.get("reference"),
        "name": p.get("name"),
        "price": p.get("price"),
        "category_id": p.get("category_id"),
        "ean": p.get("ean"),
    }, indent=2))
