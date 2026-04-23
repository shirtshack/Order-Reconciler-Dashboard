"""Sample one Shopify order + its matching Printify order, dump shapes for inspection.

Searches ALL Printify order statuses (not just on-hold) so we can understand how
Printify represents orders in its "Other orders" bucket. Prints only non-PII
structural info; saves full JSON to scripts/sample_*.json (gitignored).

Usage:
    python scripts/sample_orders.py 15802 --store steelhorse
    python scripts/sample_orders.py 1132                 # defaults to divotclub
"""
import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from printify_send.clients.printify import PrintifyClient
from printify_send.clients.shopify import ShopifyClient

parser = argparse.ArgumentParser()
parser.add_argument("order_name", nargs="?", default="1132")
parser.add_argument("--store", default="divotclub")
parser.add_argument("--max", type=int, default=300, help="Max Printify orders to search")
args = parser.parse_args()

HERE = Path(__file__).resolve().parent
cfg = tomllib.loads((HERE.parent / ".streamlit" / "secrets.toml").read_text(encoding="utf-8"))
sh = ShopifyClient(cfg["shopify"][args.store])
pr = PrintifyClient(cfg["printify"][args.store])


def dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


# ---------- Shopify ----------
print(f"# Fetching Shopify order #{args.order_name} from {args.store}...")
import httpx
token = sh._get_token()
r = httpx.get(
    f"https://{sh.store_domain}/admin/api/{sh.api_version}/orders.json",
    params={"status": "any", "name": f"#{args.order_name}"},
    headers={"X-Shopify-Access-Token": token},
    timeout=10,
)
r.raise_for_status()
orders = r.json()["orders"]
if not orders:
    sys.exit(f"No Shopify order named #{args.order_name} found on {args.store}.")
so = orders[0]

print(f"\n## Shopify {so['name']}")
print(f"  id={so['id']}")
print(f"  financial_status={so.get('financial_status')!r}")
print(f"  fulfillment_status={so.get('fulfillment_status')!r}")
print(f"  created_at={so['created_at']}")
print(f"  line_items ({len(so['line_items'])}):")
for li in so["line_items"]:
    print(
        f"    - id={li['id']}"
        f"  sku={li.get('sku')!r}"
        f"  qty={li['quantity']}"
        f"  name={li['name']!r}"
    )
dump(HERE / f"sample_{args.store}_shopify_{args.order_name}.json", so)
print(f"  (full JSON -> scripts/sample_{args.store}_shopify_{args.order_name}.json)")

# ---------- Printify (all statuses) ----------
target_shop_id = str(so["id"])
target_label = so["name"]
print(f"\n# Searching Printify orders (all statuses) for shop_order_id={target_shop_id}...")
match = None
seen = 0
for po in pr.iter_orders(status=None):
    seen += 1
    meta = po.get("metadata") or {}
    if str(meta.get("shop_order_id", "")) == target_shop_id or meta.get("shop_order_label") == target_label:
        match = po
        break
    if seen >= args.max:
        print(f"  Stopped after {seen} orders without finding a match.")
        break

if match is None:
    print(f"  No Printify order found for Shopify {target_label} in the last {seen} orders.")
    sys.exit(0)

print(f"  Matched after scanning {seen} order(s).")
print(f"\n## Printify order for {target_label}")
print(f"  top-level keys: {sorted(match.keys())}")
print(f"  id={match.get('id')!r}")
print(f"  status={match.get('status')!r}")
print(f"  sent_to_production_at={match.get('sent_to_production_at')!r}")
print(f"  fulfilment_type={match.get('fulfilment_type')!r}")
print(f"  sales_channel_type_id={match.get('sales_channel_type_id')!r}")
print(f"  created_at={match.get('created_at')}")
meta = match.get("metadata") or {}
print(f"  metadata={meta}")
li_list = match.get("line_items", [])
print(f"  line_items ({len(li_list)}):")
for li in li_list:
    lmeta = li.get("metadata") or {}
    print(
        f"    - product_id={li.get('product_id')!r}"
        f"  variant_id={li.get('variant_id')!r}"
        f"  qty={li.get('quantity')}"
        f"  status={li.get('status')!r}"
        f"  sku={lmeta.get('sku')!r}"
    )
dump(HERE / f"sample_{args.store}_printify_{args.order_name}.json", match)
print(f"  (full JSON -> scripts/sample_{args.store}_printify_{args.order_name}.json)")
