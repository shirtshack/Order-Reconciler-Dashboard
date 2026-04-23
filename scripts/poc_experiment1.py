"""POC experiment 1: skip publish, call publishing_succeeded directly on a draft.

Hypothesis: Printify's Shopify integration fires on publish.json. If we never call
publish.json, the integration never intercepts. Calling publishing_succeeded directly
on a draft Printify product might set its external link to our chosen Shopify product.
"""
import base64
import sys
import tomllib
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from printify_send.clients.printify import PrintifyClient
from printify_send.clients.shopify import ShopifyClient

STORE = "divotclub"
SHOPIFY_HANDLE = "president-faces-caddy-1-t-au"
TEMPLATE_PRODUCT_ID = "69d75f576e732a90a806d127"
DESIGN_PNG = Path(
    r"Z:\001-600-sorted by mike PNG 2024\800 PNG\Divot Club\uploaded\Famous 2\art1\PRESIDENT FACES CADDY 1.png"
)

cfg = tomllib.loads((ROOT / ".streamlit" / "secrets.toml").read_text(encoding="utf-8"))
sh = ShopifyClient(cfg["shopify"][STORE])
pr = PrintifyClient(cfg["printify"][STORE])
PRINTIFY_BASE = "https://api.printify.com/v1"


def shopify_product_count() -> int:
    r = httpx.get(
        f"https://{sh.store_domain}/admin/api/{sh.api_version}/products/count.json",
        headers={"X-Shopify-Access-Token": sh._get_token()},
        timeout=15,
    )
    return r.json()["count"]


count_before = shopify_product_count()
print(f"Shopify product count BEFORE experiment: {count_before}")

# 1. Find Shopify product
print("\n[1] Shopify lookup...")
r = httpx.get(
    f"https://{sh.store_domain}/admin/api/{sh.api_version}/products.json",
    params={"handle": SHOPIFY_HANDLE, "limit": 1},
    headers={"X-Shopify-Access-Token": sh._get_token()},
    timeout=15,
)
shopify_prod = r.json()["products"][0]
print(f"    shopify_id={shopify_prod['id']}")

# 2. Template
print("\n[2] Template fetch...")
r = httpx.get(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{TEMPLATE_PRODUCT_ID}.json",
    headers=pr._headers(), timeout=30,
)
template = r.json()
print(f"    bp={template['blueprint_id']} pp={template['print_provider_id']}")

# 3. Upload PNG
print("\n[3] PNG upload...")
contents = base64.b64encode(DESIGN_PNG.read_bytes()).decode("ascii")
r = httpx.post(
    f"{PRINTIFY_BASE}/uploads/images.json",
    headers=pr._headers(),
    json={"file_name": DESIGN_PNG.name, "contents": contents},
    timeout=180,
)
image_id = r.json()["id"]
print(f"    image_id={image_id}")

# 4. Build + create product (SAME as before: only include placeholders that have images)
new_print_areas = []
for pa in template.get("print_areas") or []:
    new_pa = {"variant_ids": pa.get("variant_ids"), "placeholders": []}
    for ph in pa.get("placeholders") or []:
        template_images = ph.get("images") or []
        if not template_images:
            continue
        new_pa["placeholders"].append(
            {
                "position": ph.get("position"),
                "images": [
                    {"id": image_id, "x": img.get("x", 0.5), "y": img.get("y", 0.5),
                     "scale": img.get("scale", 1), "angle": img.get("angle", 0)}
                    for img in template_images
                ],
            }
        )
    new_print_areas.append(new_pa)

new_variants = [
    {"id": v["id"], "price": v["price"], "is_enabled": v.get("is_enabled", True)}
    for v in (template.get("variants") or [])
]

print("\n[4] Create Printify product (as draft, no publish)...")
r = httpx.post(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products.json",
    headers=pr._headers(),
    json={
        "title": shopify_prod["title"],
        "description": template.get("description") or "",
        "blueprint_id": template["blueprint_id"],
        "print_provider_id": template["print_provider_id"],
        "variants": new_variants,
        "print_areas": new_print_areas,
    },
    timeout=60,
)
new_product = r.json()
new_product_id = new_product["id"]
print(f"    new_product_id={new_product_id}")
print(f"    visible={new_product.get('visible')}  external={new_product.get('external')}")

# 5. SKIP publish. Go straight to publishing_succeeded with our chosen shopify id.
print("\n[5] publishing_succeeded directly (NO publish call first)...")
r = httpx.post(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{new_product_id}/publishing_succeeded.json",
    headers=pr._headers(),
    json={"external": {
        "id": str(shopify_prod["id"]),
        "handle": f"https://{sh.store_domain}/products/{shopify_prod['handle']}",
    }},
    timeout=60,
)
print(f"    status={r.status_code} body={r.text[:200]!r}")

# 6. Check state after
print("\n[6] Re-fetch the new Printify product to see its state...")
r = httpx.get(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{new_product_id}.json",
    headers=pr._headers(), timeout=30,
)
np = r.json()
print(f"    visible={np.get('visible')}")
print(f"    external={np.get('external')}")
print(f"    sales_channel_properties={np.get('sales_channel_properties')}")

count_after = shopify_product_count()
print(f"\nShopify product count AFTER experiment: {count_after}  (delta={count_after - count_before})")

# --- VERDICT ---
print("\n" + "=" * 60)
external = np.get("external") or {}
success = (
    count_after == count_before
    and external.get("id") == str(shopify_prod["id"])
)
if success:
    print("SUCCESS: Printify product linked to the EXISTING Shopify product;")
    print("         no new Shopify product was created.")
    print("         => publishing_succeeded-without-publish works for linking!")
else:
    print("FAILURE:")
    if count_after != count_before:
        print(f"  - Shopify product count changed ({count_before} -> {count_after})")
    ext_id = external.get("id")
    if ext_id != str(shopify_prod["id"]):
        print(f"  - External link not set to our target (got {ext_id!r}, expected {shopify_prod['id']!r})")
print("=" * 60)

# 7. Cleanup the Printify product
print(f"\nDeleting Printify product {new_product_id} for cleanup...")
r = httpx.delete(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{new_product_id}.json",
    headers=pr._headers(), timeout=30,
)
print(f"    delete status={r.status_code}")
