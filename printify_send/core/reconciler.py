"""Compare a Printify order's line-item totals against its source Shopify order.

Matching rule: Printify's Shopify integration populates `metadata.shop_order_id` with
the Shopify order's numeric id. If total quantity on the Printify side is less than on
the Shopify side, the missing items are unlinked products that Printify's integration
silently dropped — the order must not be sent until they're linked manually.
"""
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ReconcileResult:
    printify_order_id: str
    shop_order_id: str | None
    shop_order_label: str
    shopify_found: bool
    shopify_total_qty: int
    printify_total_qty: int
    unlinked_qty: int
    is_safe_to_send: bool
    shopify_items: list[dict] = field(default_factory=list)
    printify_items: list[dict] = field(default_factory=list)
    error: str | None = None


def reconcile(
    printify_order: dict,
    get_shopify_order: Callable[[str], dict | None],
) -> ReconcileResult:
    """Reconcile one Printify order against Shopify.

    `get_shopify_order` is a callable taking a Shopify order id (string) and returning
    the order dict or None if not found. Pass either `ShopifyClient.get_order` for
    on-demand single fetches, or a closure over a prefetched dict for bulk scenarios.
    """
    meta = printify_order.get("metadata") or {}
    shop_order_id = meta.get("shop_order_id")
    shop_order_label = meta.get("shop_order_label", "")
    printify_items = printify_order.get("line_items") or []
    printify_total = sum(int(li.get("quantity", 0)) for li in printify_items)
    printify_order_id = str(printify_order.get("id", ""))

    if not shop_order_id:
        return ReconcileResult(
            printify_order_id=printify_order_id,
            shop_order_id=None,
            shop_order_label=shop_order_label,
            shopify_found=False,
            shopify_total_qty=0,
            printify_total_qty=printify_total,
            unlinked_qty=0,
            is_safe_to_send=False,
            printify_items=printify_items,
            error="No shop_order_id in Printify metadata",
        )

    shopify_order = get_shopify_order(str(shop_order_id))
    if shopify_order is None:
        return ReconcileResult(
            printify_order_id=printify_order_id,
            shop_order_id=shop_order_id,
            shop_order_label=shop_order_label,
            shopify_found=False,
            shopify_total_qty=0,
            printify_total_qty=printify_total,
            unlinked_qty=0,
            is_safe_to_send=False,
            printify_items=printify_items,
            error=f"Shopify order {shop_order_id} not found",
        )

    shopify_items = shopify_order.get("line_items") or []
    shopify_total = sum(int(li.get("quantity", 0)) for li in shopify_items)
    diff = shopify_total - printify_total

    return ReconcileResult(
        printify_order_id=printify_order_id,
        shop_order_id=shop_order_id,
        shop_order_label=shop_order_label,
        shopify_found=True,
        shopify_total_qty=shopify_total,
        printify_total_qty=printify_total,
        unlinked_qty=max(0, diff),
        is_safe_to_send=(diff == 0),
        shopify_items=shopify_items,
        printify_items=printify_items,
    )
