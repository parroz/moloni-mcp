"""Manual smoke test: list_products_by_category against live Moloni."""
import asyncio
import time

from moloni_mcp_server import _list_products_by_category_uncached

AMERICAN_VINTAGE_CATEGORY_ID = 6549313
KNOWN_REFERENCE = "AMV-BOBY03FE26-TURQUOISE-M"
KNOWN_PRODUCT_ID = 229892963


async def main() -> None:
    t0 = time.perf_counter()
    flat = await _list_products_by_category_uncached(
        AMERICAN_VINTAGE_CATEGORY_ID, False
    )
    flat_elapsed = time.perf_counter() - t0
    print(f"recursive=False: {len(flat)} products in {flat_elapsed:.2f}s")

    t1 = time.perf_counter()
    rec = await _list_products_by_category_uncached(
        AMERICAN_VINTAGE_CATEGORY_ID, True
    )
    rec_elapsed = time.perf_counter() - t1
    print(f"recursive=True: {len(rec)} products in {rec_elapsed:.2f}s")

    assert len(rec) > len(flat), "recursive should include subcategory products"
    by_id = {p["product_id"]: p for p in rec}
    assert KNOWN_PRODUCT_ID in by_id, f"missing product_id {KNOWN_PRODUCT_ID}"
    assert by_id[KNOWN_PRODUCT_ID]["reference"] == KNOWN_REFERENCE
    print(f"Found {KNOWN_REFERENCE} (product_id {KNOWN_PRODUCT_ID}) OK")


if __name__ == "__main__":
    asyncio.run(main())
