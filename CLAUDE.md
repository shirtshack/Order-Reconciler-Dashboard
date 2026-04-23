# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Streamlit dashboard + CLI for Shopify → Printify order reconciliation across 5 paired store accounts. Detects when Printify's Shopify integration has silently dropped unlinked items from an on-hold order (so sending to production would ship partial) and provides a per-store bulk "send all OK orders" button.

The folder is named `divotclub/` for historical reasons — it started as a single-store project. It now handles all 5 stores via keyed `[shopify.<key>]` / `[printify.<key>]` sections in `secrets.toml`. Renaming the folder is deferred to avoid breakage; don't bother unless asked.

## Stack

- Python 3.11+ (stdlib `tomllib` for config)
- Streamlit (UI)
- httpx (both APIs)
- `pydantic` is in requirements.txt but currently unused; fine to leave or remove.

## Project layout

```
app.py                      # Streamlit dashboard (reactive reconciler + send button)
scripts/
  test_connection.py        # iterates every [shopify.*] + [printify.*] section, verifies tokens
  sample_orders.py          # one-off: dump raw shape of one Printify order + its Shopify match
  reconcile_all.py          # CLI: --store <key> --days <N>  (default 3)
  sync_products.py          # PROACTIVE LINKER: pre-creates Printify drafts for Shopify products
  poc_*.py                  # one-off experiments (link-without-publish POCs) — kept for reference
printify_send/
  clients/
    shopify.py              # ShopifyClient: client_credentials grant, token cache, bulk get_orders
    printify.py             # PrintifyClient: list/iter_orders, get_order, send_to_production
  core/
    reconciler.py           # reconcile(printify_order, get_shopify_order) -> ReconcileResult
.streamlit/
  secrets.toml              # gitignored — real credentials for all 5 stores
  secrets.toml.example      # template with REPLACE_ME placeholders
```

## Stores configured

`divotclub`, `velotees`, `ohmycod`, `togsy`, `steelhorse`. Each has matching `[shopify.<key>]` and `[printify.<key>]` sections in `secrets.toml`.

## Common commands

```bash
# Run the dashboard. Port 8501 is in use on the user's Windows machine — use 8502.
python -m streamlit run app.py --server.port=8502

# Verify every store's credentials in one go
python scripts/test_connection.py

# CLI reconcile for one store
python scripts/reconcile_all.py --store steelhorse --days 3

# Proactive linker — create Printify drafts for Shopify products
# Dry-run by default; --execute required to modify Printify.
python scripts/sync_products.py --store divotclub --vendor "12-mar-t AU"            # single vendor dry-run
python scripts/sync_products.py --store divotclub --execute --skip-linking          # full catalogue, create drafts
python scripts/sync_products.py --store divotclub --vendor "batch 2 AU" --execute --skip-linking --image-url-prefix "https://storage.googleapis.com/divotclub-images/Golf Art batch 2"
```

## Secrets schema

- **Shopify** per store: `store_domain` (the `*.myshopify.com`), `client_id`, `client_secret`, `api_version` (`2026-04`). Credentials come from the Shopify Dev Dashboard → app → Settings. The `client_credentials` OAuth grant exchanges them for short-lived `shpat_` tokens at runtime (`ShopifyClient` caches until ~1 min before expiry).
- **Printify** per store: `access_token` (long-lived Bearer PAT from Printify → Connections), `shop_id` (numeric — auto-discovered by `test_connection.py` on first run).

The Shopify Dev Dashboard is immutable-per-version; adding scopes requires creating a new version, releasing it, and re-installing the app to the store.

## Architecture notes

**Order matching.** Printify's Shopify integration populates `metadata.shop_order_id` (numeric Shopify id) and `metadata.shop_order_label` (`#NNNN`) on every imported order. `external_id` is NOT populated — don't rely on it. Reconciler matches via `shop_order_id`.

**Detection rule.** Printify silently drops unlinked line items. Compare `sum(qty)` on Shopify vs Printify; equal = safe, Shopify > Printify = unlinked items exist. Line-level matching by SKU does NOT work because Shopify uses merchant-defined SKUs (`Einstein-Teeing-Off-6-T-US-Navy-XL`) and Printify has its own numeric SKUs — so the tool reports totals, not per-item diff.

**Bulk Shopify fetch.** `reconcile_store` collects all in-window Printify orders first, then bulk-fetches their matching Shopify orders via `/admin/api/{v}/orders.json?ids=...&status=any` (batched 250 per request). Before this, Steel Horse with many on-hold orders would have taken minutes; now runs in seconds. `reconcile()` takes a `get_shopify_order` callable so either single-fetch (`sh.get_order`) or prefetched-dict (`shopify_by_id.get`) works.

**`--days` filter.** Printify doesn't purge old on-hold orders — velotees had 200+ stale orders going back to #6338. The reconcile loop stops at the first order older than `cutoff`. Default 3 days covers the weekend-off worst case; shorter for faster testing, bump higher only if the user was away longer.

**Rate limiting.** Shopify REST is a 40-deep leaky bucket with 2/sec refill. With bulk fetch we make 1-2 Shopify calls per store. 429 handling respects `Retry-After`. Token exchange counts against the bucket but happens once per 24h due to caching.

## Proactive linker (`scripts/sync_products.py`)

Second major piece after the reactive dashboard. Creates Printify draft products ahead of time for Shopify products so orders don't land in Printify's "Other orders" tab.

**Core flow per Shopify product:**
1. Look up Printify product template by `(store, country)` — templates hardcoded at module top.
2. Find design PNG on disk (or pull via URL if `--image-url-prefix` given).
3. Upload to Printify `/uploads/images.json`.
4. Create Printify product using template's blueprint/variants with the new image in the `front` placeholder only. `y` is computed from image dimensions so top of design lands near top of print box (user prefers ~2% below top).
5. Optionally call `publishing_succeeded` with the Shopify product id to pre-link (default: skip — `--skip-linking` creates pure drafts that user migrates manually via Printify UI).

**Critical constants (edit when adding stores):**

```python
TEMPLATES = {
    ("divotclub", "AU"): "69d75f576e732a90a806d127",   # yes retirement plan GOLF T-Shirt AU (bp=145, pp=34)
    ("divotclub", "US"): "69e1e8911c7619bbad06fef2",   # Einstein Teeing Off 6 T-Shirt US (bp=145, pp=29)
    # Add (velotees, AU), (ohmycod, US), etc. as each store is rolled out.
}

PRIMARY_COLOR_OPTION_ID = {"AU": 364, "US": 418}  # 364 = Military Green, 418 = Black (Gildan blueprint)

PNG_ROOTS = [
    Path(r"Z:\001-600-sorted by mike PNG 2024\800 PNG"),
    Path(r"C:\001-600-sorted by mike PNG 2024\Divot Club Golf"),
]
```

**Filters & matching:**
- Vendor filter (`--vendor`) is optional; absent = entire Shopify catalogue.
- Country detected from title suffix (` T-Shirt AU`, ` T Shirt US`, etc.); UK products auto-skipped (no template).
- PNG filename = title-minus-suffix + `.png`; case-insensitive local index; prefer paths containing `\art1\` or `\Art 1\`.
- Already-migrated products detected TWO ways (both needed): `external.id` matches Shopify id, OR a Printify product with matching normalised title exists (UI-migrated products have empty `external.id`).

**URL-based uploads (`--image-url-prefix`):** if PNGs are publicly readable at `https://host/prefix/<stem>.png`, Printify fetches them server-side — bypasses outbound bandwidth. Must URL-encode the FULL path (prefix + filename together). If host is `storage.googleapis.com`, the script pre-lists the bucket via the JSON API to skip products whose PNG isn't there.

**Parallelism (`--workers N`, default 5):**
- Small PNGs (<3 MB): 5 workers fine.
- Big PNGs (5-10 MB): 2 workers — higher counts saturate upload bandwidth, trigger Printify 502s.
- URL mode: can safely push to 5-10 (no local bandwidth competition).
- Upload and create both retry on 5xx with exponential backoff (2s, 4s, 8s, 16s).

**Never delete linked Printify products.** Cascade deletes the Shopify product — confirmed by losing `PRESIDENT FACES CADDY 1 T-Shirt AU` (irrecoverable).

## Send-to-production flow

Per-store bulk send in the Streamlit dashboard: user ticks "Confirm: send N OK order(s)" checkbox to enable the button, clicks "Send N to production". On click: iterate safe results, POST `send_to_production.json` per order, stash successes+failures in `st.session_state[last_send_{store}]`, clear the reconcile cache, `st.rerun()`. The stash is popped + rendered at the top of the store block on the next run, and the checkbox key is reset there — which is the only legal place to reset a widget's session_state (BEFORE the widget is re-instantiated).

## Out of scope — deliberately

- **Merch Panda orders** (UK / EU self-fulfilled). The tool only sees orders that flow into Printify. Shopify orders fulfilled via Merch Panda never reach Printify and will not appear in the dashboard. This is intentional — do not add Shopify-side unfulfilled-order browsing without the user asking.
- The stale-on-hold backlog in Printify. `--days` filter hides it and that's the right answer.

## Current status (2026-04-23)

- **Divot Club fully synced**: 1,356 drafts created across the whole catalogue in one ~80-minute run (workers=3, skip-linking). 0 failures, thanks to retry-on-5xx. Plus ~547 pre-existing UI-migrated products already in Printify. Covers essentially every Printify-eligible Shopify product.
- **Velotees, Oh My Cod, Togsy, Steel Horse**: not yet synced. Each needs a pair of templates added to `TEMPLATES` dict before running.
- Reactive dashboard + send button: in use.
- Reconciler run against live data repeatedly; detection logic validated.

## Planned work (queued)

1. **Roll sync to the other 4 stores.** Each needs:
   - One existing UI-linked Printify product per country (AU + US) to use as the template — user picks or Claude picks automatically from first AU/US match.
   - Confirm PNG folder roots contain that store's designs (velotees / ohmycod / togsy / steelhorse each had their own subfolders under `Z:\801 PNG\`).
2. **User's per-product manual follow-up** (thousands of clicks, best done incrementally as orders arrive):
   - Click "Migrate product" on each draft in Printify's External Products tab (~1 click each)
   - Set Military Green as primary mockup on AU drafts (can't be automated — API doesn't expose it)
3. **Credential rotation.** 10 tokens pasted in chat during the build session — rotate eventually (Shopify: Dev Dashboard → Settings → Rotate; Printify: Connections → delete + recreate PAT).
4. **Linker for live-order UNLINKED cases.** When the reactive dashboard flags a Printify order as UNLINKED (missing items), the fix would be to find/create a Printify product for the missing item AND add it to the existing on-hold order as a line item. Step 2 (add to existing order) is NOT publicly documented on Printify's API — needs investigation before building.
5. **Streamlit Cloud hosting** if remote access is wanted. Would move `secrets.toml` into Streamlit Cloud's secrets manager.

## Gotchas

- **Do not `@st.cache_resource` on `load_cfg()`.** We removed it so new stores in `secrets.toml` appear on the next rerun without restarting the Streamlit server. Re-adding the cache breaks that.
- **Multiselect selection persists.** When a new store is added to `secrets.toml`, the user has to open the Stores multiselect and tick the new entry — the `default=ALL_STORES` is only applied on first render.
- **Widget session_state rule.** You cannot assign to a widget's key AFTER the widget has been instantiated in the current Streamlit run. Only do widget-key assignments BEFORE the widget is re-instantiated on a subsequent run (see the "carry-over from previous send" block at the top of the per-store loop in `app.py`).
