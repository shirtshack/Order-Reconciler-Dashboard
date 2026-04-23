"""POC: Create a linked Printify product without overwriting the Shopify listing.

Target case:
  - Shopify product: "PRESIDENT FACES CADDY 1 T-Shirt AU" on Divot Club (already exists,
    with its own mockup images and variants)
  - Template Printify product: 69d75f576e732a90a806d127 (yes retirement plan GOLF T-Shirt AU)
  - Design PNG: see DESIGN_PNG constant below

Sequence:
  1. Find Shopify product by handle.
  2. Fetch template Printify product (to copy blueprint/variants/print_areas structure).
  3. Upload design PNG to Printify -> get image id.
  4. POST /products.json with template's structure + new image id + new title.
  5. POST /publish.json with every flag False (goal: no-op on Shopify side).
  6. POST /publishing_succeeded.json with the existing Shopify product's id to
     establish the link manually.

Success criteria (verify in Printify + Shopify UIs after running):
  - New Printify product appears under "My Products" (linked), not "External products".
  - Shopify product's images, description, variants are NOT changed.
  - Printify product's product_id / external id references the existing Shopify product id.

Failure modes to watch for:
  - publish.json response code / body
  - Shopify-side changes (images replaced, description rewritten, new variants)
  - Dup product appearing in Shopify
"""
import base64
import json
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


def fail(msg: str, r: httpx.Response | None = None) -> None:
    print(f"\n!! {msg}")
    if r is not None:
        print(f"   status={r.status_code}")
        print(f"   body={r.text[:800]}")
    sys.exit(1)


# --- 1. Find the Shopify product ---
print("[1/6] Looking up Shopify product...")
token = sh._get_token()
r = httpx.get(
    f"https://{sh.store_domain}/admin/api/{sh.api_version}/products.json",
    params={"handle": SHOPIFY_HANDLE, "limit": 1},
    headers={"X-Shopify-Access-Token": token},
    timeout=15,
)
if r.status_code != 200:
    fail("Shopify products.json lookup failed", r)
products = r.json().get("products") or []
if not products:
    fail(f"No Shopify product with handle '{SHOPIFY_HANDLE}'")
shopify_prod = products[0]
print(f"      Shopify id={shopify_prod['id']}  title='{shopify_prod['title']}'")
print(f"      variants={len(shopify_prod.get('variants') or [])}  images={len(shopify_prod.get('images') or [])}")

# --- 2. Fetch template Printify product ---
print(f"\n[2/6] Fetching template Printify product {TEMPLATE_PRODUCT_ID}...")
r = httpx.get(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{TEMPLATE_PRODUCT_ID}.json",
    headers=pr._headers(),
    timeout=30,
)
if r.status_code != 200:
    fail("Template fetch failed", r)
template = r.json()
print(f"      Template: '{template['title']}'")
print(f"      blueprint_id={template['blueprint_id']}  print_provider_id={template['print_provider_id']}")
print(f"      variants={len(template.get('variants') or [])}  print_areas={len(template.get('print_areas') or [])}")

# --- 3. Upload PNG ---
if not DESIGN_PNG.exists():
    fail(f"Design PNG not found: {DESIGN_PNG}")
size_mb = DESIGN_PNG.stat().st_size / 1_000_000
print(f"\n[3/6] Uploading design PNG ({size_mb:.1f} MB)...")
contents = base64.b64encode(DESIGN_PNG.read_bytes()).decode("ascii")
r = httpx.post(
    f"{PRINTIFY_BASE}/uploads/images.json",
    headers=pr._headers(),
    json={"file_name": DESIGN_PNG.name, "contents": contents},
    timeout=180,
)
if r.status_code not in (200, 201):
    fail("Upload failed", r)
upload_resp = r.json()
image_id = upload_resp.get("id")
print(f"      image_id={image_id}")

# --- 4. Create new Printify product (copy template structure, swap in new image) ---
print(f"\n[4/6] Creating new Printify product...")
new_print_areas = []
for pa in template.get("print_areas") or []:
    new_pa = {"variant_ids": pa.get("variant_ids"), "placeholders": []}
    for ph in pa.get("placeholders") or []:
        template_images = ph.get("images") or []
        if not template_images:
            continue  # template has no image in this placeholder — don't add one here
        new_pa["placeholders"].append(
            {
                "position": ph.get("position"),
                "images": [
                    {
                        "id": image_id,
                        "x": img.get("x", 0.5),
                        "y": img.get("y", 0.5),
                        "scale": img.get("scale", 1),
                        "angle": img.get("angle", 0),
                    }
                    for img in template_images
                ],
            }
        )
    new_print_areas.append(new_pa)

new_variants = [
    {"id": v["id"], "price": v["price"], "is_enabled": v.get("is_enabled", True)}
    for v in (template.get("variants") or [])
]

new_payload = {
    "title": shopify_prod["title"],  # match Shopify title
    "description": template.get("description") or "",
    "blueprint_id": template["blueprint_id"],
    "print_provider_id": template["print_provider_id"],
    "variants": new_variants,
    "print_areas": new_print_areas,
}

r = httpx.post(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products.json",
    headers=pr._headers(),
    json=new_payload,
    timeout=60,
)
if r.status_code not in (200, 201):
    fail("Product create failed", r)
new_product = r.json()
new_product_id = new_product.get("id")
print(f"      new Printify product_id={new_product_id}")

# --- 5. Publish with all flags false (trying to trigger 'publishing' state only) ---
print(f"\n[5/6] Publishing with all flags False...")
r = httpx.post(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{new_product_id}/publish.json",
    headers=pr._headers(),
    json={
        "title": False,
        "description": False,
        "images": False,
        "variants": False,
        "tags": False,
        "keyFeatures": False,
        "shipping_template": False,
    },
    timeout=60,
)
print(f"      publish.json status={r.status_code}  body={r.text[:200]!r}")
if r.status_code not in (200, 201, 204):
    fail("Publish failed", r)

# --- 6. publishing_succeeded with the existing Shopify product id ---
print(f"\n[6/6] Calling publishing_succeeded to establish link...")
shopify_handle_url = f"https://{sh.store_domain}/products/{shopify_prod['handle']}"
r = httpx.post(
    f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{new_product_id}/publishing_succeeded.json",
    headers=pr._headers(),
    json={"external": {"id": str(shopify_prod["id"]), "handle": shopify_handle_url}},
    timeout=60,
)
print(f"      publishing_succeeded status={r.status_code}  body={r.text[:200]!r}")
if r.status_code not in (200, 201, 204):
    fail("publishing_succeeded failed", r)

print("\n" + "=" * 60)
print("POC COMPLETE — verify the following manually:")
print(f"  Printify product id : {new_product_id}")
print(f"  Linked Shopify id   : {shopify_prod['id']}")
print()
print("  In Printify UI (Divot Club shop):")
print("    - Is the new product in 'My Products' (not 'External products')?")
print(f"    - Does it appear linked to {shopify_prod['title']}?")
print()
print("  In Shopify admin:")
print(f"    - Is the Shopify product '{shopify_prod['title']}' unchanged?")
print("    - No new images added?")
print("    - Description / variants untouched?")
print("=" * 60)
