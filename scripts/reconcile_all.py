"""List every on-hold Printify order with its Shopify reconciliation status.

    python scripts/reconcile_all.py                  # defaults to --store divotclub
    python scripts/reconcile_all.py --store velotees
"""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from printify_send.clients.printify import PrintifyClient
from printify_send.clients.shopify import ShopifyClient
from printify_send.config import load_cfg
from printify_send.core.reconciler import reconcile

parser = argparse.ArgumentParser()
parser.add_argument("--store", default="divotclub", help="Store key in secrets.toml (e.g. divotclub, velotees)")
parser.add_argument("--days", type=int, default=3, help="Only look at Printify orders created in the last N days (default 3 — covers a weekend)")
args = parser.parse_args()

cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

cfg = load_cfg()

try:
    shopify_cfg = cfg["shopify"][args.store]
    printify_cfg = cfg["printify"][args.store]
except KeyError:
    sys.exit(
        f"No config for store '{args.store}'. "
        f"Available: shopify={list(cfg.get('shopify', {}))}, printify={list(cfg.get('printify', {}))}"
    )

sh = ShopifyClient(shopify_cfg)
pr = PrintifyClient(printify_cfg)

print(f"Reconciling store: {args.store}  (shopify={shopify_cfg['store_domain']}, printify shop_id={printify_cfg['shop_id']})")
print(f"Cutoff: Printify orders created after {cutoff.date()} (last {args.days} days)")

# Phase 1: collect Printify on-hold orders in window
printify_orders = []
for po in pr.iter_orders(status="on-hold"):
    created_str = po.get("created_at")
    if isinstance(created_str, str):
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    else:
        created = created_str
    if created and created < cutoff:
        break
    printify_orders.append(po)
print(f"Found {len(printify_orders)} Printify on-hold order(s) in window.")

# Phase 2: bulk-fetch matching Shopify orders (1 call per 250 orders)
shop_ids = [
    str((po.get("metadata") or {}).get("shop_order_id"))
    for po in printify_orders
    if (po.get("metadata") or {}).get("shop_order_id")
]
print(f"Bulk-fetching {len(shop_ids)} Shopify order(s)...")
shopify_by_id = sh.get_orders(shop_ids) if shop_ids else {}

# Phase 3: reconcile with prefetched lookup
results = [reconcile(po, shopify_by_id.get) for po in printify_orders]

if not results:
    print("\nNo on-hold Printify orders.")
    sys.exit(0)

safe = [r for r in results if r.is_safe_to_send]
unsafe = [r for r in results if not r.is_safe_to_send]

print(
    f"\n{len(results)} on-hold order(s):  "
    f"{len(safe)} safe to send, {len(unsafe)} need attention\n"
)
print(
    f"{'STATUS':<9} {'LABEL':<10} {'SHOP QTY':>8} {'PR QTY':>7} {'MISSING':>8}  PRINTIFY ID"
)
print("-" * 78)
for r in sorted(results, key=lambda x: (x.is_safe_to_send, x.shop_order_label)):
    if r.error:
        status = "ERROR"
    elif r.is_safe_to_send:
        status = "OK"
    else:
        status = "UNLINKED"
    print(
        f"{status:<9} {r.shop_order_label:<10} {r.shopify_total_qty:>8} "
        f"{r.printify_total_qty:>7} {r.unlinked_qty:>8}  {r.printify_order_id}"
    )
    if r.error:
        print(f"          └─ {r.error}")
