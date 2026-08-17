"""Streamlit dashboard for Shopify -> Printify order reconciliation.

    streamlit run app.py
"""
import hmac
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from printify_send.clients.printify import PrintifyClient
from printify_send.clients.shopify import ShopifyClient
from printify_send.config import EXCLUDED_STORES, all_stores, load_cfg
from printify_send.core.reconciler import ReconcileResult, reconcile

st.set_page_config(page_title="Order Reconciler", layout="wide")


def check_password() -> None:
    if st.session_state.get("authed"):
        return
    st.title("Order Reconciler")
    expected = st.secrets.get("auth", {}).get("password", "")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if expected and hmac.compare_digest(pw, expected):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password")
    st.stop()


check_password()


# Store credentials come from dev/api-credentials/secrets.toml via config.load_cfg.
# st.secrets is passed only as the fallback for Streamlit Community Cloud, where that
# path doesn't exist; it isn't parsed when the central file is reachable. The [auth]
# password in check_password() above is separate and always reads st.secrets.
cfg = load_cfg(fallback=st.secrets)
ALL_STORES = all_stores(cfg)


@st.cache_data(ttl=60, show_spinner=False)
def reconcile_store(store_key: str, days: int) -> tuple[list[ReconcileResult], datetime]:
    sh = ShopifyClient(cfg["shopify"][store_key])
    pr = PrintifyClient(cfg["printify"][store_key])
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # 1. Collect all Printify on-hold orders within the window
    printify_orders: list[dict] = []
    for po in pr.iter_orders(status="on-hold"):
        created_str = po.get("created_at")
        if isinstance(created_str, str):
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        else:
            created = created_str
        if created and created < cutoff:
            break
        printify_orders.append(po)

    # 2. Bulk-fetch the matching Shopify orders in one (or few) calls
    shop_ids = [
        str((po.get("metadata") or {}).get("shop_order_id"))
        for po in printify_orders
        if (po.get("metadata") or {}).get("shop_order_id")
    ]
    shopify_by_id = sh.get_orders(shop_ids) if shop_ids else {}

    # 3. Reconcile with prefetched lookup
    results = [reconcile(po, shopify_by_id.get) for po in printify_orders]
    return results, datetime.now(timezone.utc)


with st.sidebar:
    st.subheader("Filters")
    days = st.slider("Days to look back", 1, 14, 3)
    # The multiselect's selection persists for the life of the browser session, so a
    # `default` only bites on the first render — a store credentialed afterwards stays
    # unticked and reads as "missing" from the dashboard, with nothing to flag it.
    # Tick any newly discovered store BEFORE the widget is instantiated, which is the
    # only legal point to write a widget's session_state key.
    if "store_pick" not in st.session_state:
        st.session_state.store_pick = ALL_STORES
        st.session_state.stores_seen = set(ALL_STORES)
    else:
        fresh = set(ALL_STORES) - st.session_state.get("stores_seen", set())
        if fresh:
            picked = set(st.session_state.store_pick) | fresh
            st.session_state.store_pick = [s for s in ALL_STORES if s in picked]
            st.session_state.stores_seen = set(ALL_STORES)

    selected_stores = st.multiselect("Stores", ALL_STORES, key="store_pick")

    # A store needs BOTH a [shopify.*] and a [printify.*] section to be reconcilable.
    # With only one it silently vanishes from the list above — the exact way a brand
    # goes unprocessed unnoticed. Say so out loud instead.
    half_wired = sorted(
        (set(cfg["shopify"]) ^ set(cfg["printify"])) - EXCLUDED_STORES
    )
    if half_wired:
        st.warning(
            "Only one API credentialed, so not reconcilable: "
            + ", ".join(half_wired)
        )
    st.caption(f"{len(ALL_STORES)} store(s) credentialed")
    st.divider()
    if st.button("Refresh (clear cache)", use_container_width=True):
        reconcile_store.clear()
        st.rerun()

st.title("Order reconciler")
st.caption("Shopify -> Printify pre-send verification")

totals_slot = st.empty()
grand_orders = 0
grand_items = 0

if not selected_stores:
    st.info("Pick at least one store in the sidebar.")
    st.stop()


def order_sort_key(r: ReconcileResult) -> tuple:
    try:
        n = int(r.shop_order_label.lstrip("#") or 0)
    except ValueError:
        n = 0
    return (r.is_safe_to_send, -n)


for store in selected_stores:
    st.divider()
    st.subheader(store)

    # Carry-over results from a send that happened on the previous run.
    # Read/reset these BEFORE the checkbox widget is instantiated below, so it's legal
    # to touch session_state for the checkbox key here.
    last_send_key = f"last_send_{store}"
    checkbox_key = f"confirm_send_{store}"
    if last_send_key in st.session_state:
        last = st.session_state.pop(last_send_key)
        st.session_state[checkbox_key] = False  # uncheck for next click
        for label in last.get("successes", []):
            st.success(f"Sent {label} to production")
        for label, err in last.get("failures", []):
            st.error(f"{label} failed - {err}")

    try:
        with st.spinner(f"Loading {store}..."):
            results, loaded_at = reconcile_store(store, days)
    except Exception as e:  # API boundary — any failure should show inline, not crash
        st.error(f"Failed to load {store}: {type(e).__name__}: {e}")
        continue

    grand_orders += len(results)
    grand_items += sum(r.printify_total_qty for r in results)

    st.caption(
        f"Loaded at {loaded_at.strftime('%H:%M:%S UTC')} - window: last {days} days"
    )

    if not results:
        st.success(f"No on-hold orders in the last {days} days.")
        continue

    safe = [r for r in results if r.is_safe_to_send]
    unsafe = [r for r in results if not r.is_safe_to_send and not r.error]
    errors = [r for r in results if r.error]

    m1, m2, m3 = st.columns(3)
    m1.metric("On-hold", len(results))
    m2.metric("Safe to send", len(safe))
    m3.metric(
        "Needs attention",
        len(unsafe) + len(errors),
        delta="blocked" if (unsafe or errors) else None,
        delta_color="inverse",
    )

    if safe:
        conf_col, btn_col = st.columns([3, 1])
        confirm = conf_col.checkbox(
            f"Confirm: send {len(safe)} OK order(s) for {store} to production",
            key=checkbox_key,
        )
        if btn_col.button(
            f"Send {len(safe)} to production",
            type="primary",
            disabled=not confirm,
            key=f"send_btn_{store}",
            use_container_width=True,
        ):
            pr = PrintifyClient(cfg["printify"][store])
            successes: list[str] = []
            failures: list[tuple[str, str]] = []
            progress = st.progress(0.0, text=f"Sending 0/{len(safe)}...")
            for i, r in enumerate(safe, start=1):
                try:
                    pr.send_to_production(r.printify_order_id)
                    successes.append(r.shop_order_label)
                except Exception as e:  # API boundary
                    failures.append((r.shop_order_label, f"{type(e).__name__}: {e}"))
                progress.progress(i / len(safe), text=f"Sending {i}/{len(safe)}...")
            progress.empty()
            # Stash results for display after the rerun — on the next run we'll pop these
            # from session_state and render them, then the checkbox reset happens legally
            # (before the widget is re-instantiated).
            st.session_state[last_send_key] = {"successes": successes, "failures": failures}
            reconcile_store.clear()
            st.rerun()

    for r in sorted(results, key=order_sort_key):
        if r.error:
            header = f":orange[ERROR]  {r.shop_order_label or '(no label)'}  -  {r.error}"
        elif r.is_safe_to_send:
            header = (
                f":green[OK]  {r.shop_order_label}  -  "
                f"{r.shopify_total_qty} item(s) ready"
            )
        else:
            header = (
                f":red[UNLINKED]  {r.shop_order_label}  -  "
                f"Shopify {r.shopify_total_qty} / Printify {r.printify_total_qty}  -  "
                f"**{r.unlinked_qty} missing**"
            )

        with st.expander(header, expanded=False):
            if r.error:
                st.warning(r.error)
                continue
            col_s, col_p = st.columns(2)
            with col_s:
                st.markdown(
                    f"**Shopify** - {len(r.shopify_items)} line(s), "
                    f"total qty {r.shopify_total_qty}"
                )
                for li in r.shopify_items:
                    st.markdown(
                        f"- {li.get('name', '')}  \n"
                        f"  qty {li.get('quantity')} - SKU `{li.get('sku', '')}`"
                    )
            with col_p:
                st.markdown(
                    f"**Printify** - {len(r.printify_items)} line(s), "
                    f"total qty {r.printify_total_qty}"
                )
                for li in r.printify_items:
                    meta = li.get("metadata") or {}
                    title = (meta.get("title") or "").strip()
                    variant = meta.get("variant_label", "")
                    st.markdown(
                        f"- {title} - {variant}  \n"
                        f"  qty {li.get('quantity')}"
                    )

with totals_slot.container():
    t1, t2 = st.columns(2)
    t1.metric("On-hold orders (all stores)", grand_orders)
    t2.metric("Items in those orders", grand_items)
