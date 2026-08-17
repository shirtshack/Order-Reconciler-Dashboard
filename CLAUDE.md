# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Streamlit dashboard + CLI for Shopify → Printify order reconciliation across every paired store account. Detects when Printify's Shopify integration has silently dropped unlinked items from an on-hold order (so sending to production would ship partial) and provides a per-store bulk "send all OK orders" button.

The folder is named `divotclub/` for historical reasons — it started as a single-store project. It now handles every store that has both a `[shopify.<key>]` and a `[printify.<key>]` section in the central credentials file. Renaming the folder is deferred to avoid breakage; don't bother unless asked.

## Stack

- Python 3.11+ (stdlib `tomllib` for config)
- Streamlit (UI)
- httpx (both APIs)
- `pydantic` is in requirements.txt but currently unused; fine to leave or remove.

## Project layout

```
app.py                      # Streamlit dashboard (reactive reconciler + send button)
scripts/
  test_connection.py            # iterates every [shopify.*] + [printify.*] section, verifies tokens
  sample_orders.py              # one-off: dump raw shape of one Printify order + its Shopify match
  reconcile_all.py              # CLI: --store <key> --days <N>  (default 3)
  sync_products.py              # PROACTIVE LINKER: pre-creates Printify drafts for Shopify products
  patch_partial_fulfillments.py # POST-FULFILLMENT PATCHER: copies Printify tracking onto orphan line items
  poc_*.py                      # one-off experiments (link-without-publish POCs) — kept for reference
printify_send/
  config.py               # load_cfg() / all_stores() — the ONLY place secrets are read.
                          #   Reads dev/api-credentials/secrets.toml; st.secrets fallback.
  clients/
    shopify.py              # ShopifyClient: client_credentials grant, token cache, bulk get_orders,
                            #   list_partially_fulfilled_orders, get_fulfillment_orders, create_fulfillment
    printify.py             # PrintifyClient: list/iter_orders, get_order, send_to_production
  core/
    reconciler.py           # reconcile(printify_order, get_shopify_order) -> ReconcileResult
    fulfillment_patcher.py  # patch_order() — find orphan line items, apply existing tracking
.streamlit/
  secrets.toml              # gitignored. [auth] password + stale fallback copy — NOT the
                            #   source of truth; see "Secrets schema" below
  secrets.toml.example      # template with REPLACE_ME placeholders
```

## Stores configured

**Auto-discovered — do not maintain a list here, and do not name stores in this repo
(see "This repo is PUBLIC" below).** Any store with BOTH a `[shopify.<key>]` and a
`[printify.<key>]` section in the central credentials file appears automatically, minus
`EXCLUDED_STORES`. 12 as of 2026-07-16; run `python scripts/test_connection.py` to see
the live list.

`featherfound` is excluded on purpose — Kennedy reconciles it herself via
`featherfound-reconciler/`, with credentials kept isolated. It has a central
`[printify.featherfound]` section, so removing the exclusion would surface it here.

## This repo is PUBLIC

`github.com/shirtshack/Order-Reconciler-Dashboard` — public, and the Streamlit Cloud app
deploys from it. Anything committed is permanently visible, including in git history.

**Never name a stealth / separate-entity store in this repo.** Per the root `CLAUDE.md`
"Brand identity isolation" section, those stores must have no traceable link to Merch
Panda or any sibling brand, and this repo sits under the `shirtshack` org — so naming one
here alongside the others is exactly the public fingerprint that rule exists to prevent.
Check a store's class in `dev/BRANDS.md` before you type its name into any file here.
The linked brands already named in this repo predate that check; don't add to them.

Nothing in the code needs a store name — the list is discovered at runtime.

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

# Post-fulfillment patcher — copy Printify tracking onto orphan line items
# Dry-run by default; --execute required to POST fulfillments to Shopify.
python scripts/patch_partial_fulfillments.py --store ohmycod                        # dry-run, last 7 days
python scripts/patch_partial_fulfillments.py --store ohmycod --days 14              # wider window
python scripts/patch_partial_fulfillments.py --store ohmycod --execute              # apply
python scripts/patch_partial_fulfillments.py --store ohmycod --execute --notify     # apply AND email customer
```

## Secrets schema

**Where credentials come from.** `dev/api-credentials/secrets.toml` — the cross-project
single source of truth — read via `printify_send/config.py`. Every consumer (`app.py` and
all `scripts/*`) goes through `load_cfg()`; nothing reads a secrets path of its own.
**Do not add credentials to this project's `.streamlit/secrets.toml`** — the root
`CLAUDE.md` no-duplicate-credentials rule applies here.

That local file survives for two things only: the `[auth]` dashboard password (which
`check_password()` still reads via `st.secrets`), and as a fallback if the central file
is unreachable. Its `[shopify.*]` / `[printify.*]` sections are now dead weight and are
kept only so the fallback path works; they are NOT read when Dropbox is synced.

This layout exists because the old per-project copy went stale silently: two stores were
fully credentialed centrally but missing from the copy, so they never appeared on the
dashboard and nothing errored (2026-07-16).

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

## Post-fulfillment patcher (`scripts/patch_partial_fulfillments.py`)

Third major piece. Fixes a recurring symptom of the manual product-edit workflow.

### Required Shopify scopes (in addition to read/write_orders)

The patcher reads `/orders/{id}/fulfillment_orders.json` and writes `/fulfillments.json`.
Shopify gates these by **three separate FO scope tiers**, only ONE of which is needed
for read/write_orders alone:

- `read_third_party_fulfillment_orders`, `write_third_party_fulfillment_orders` — REQUIRED.
  Printify is a third-party fulfillment service; its FOs are silently filtered out of
  the API response without these scopes (no 403 — just an empty list).
- `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders` —
  recommended belt-and-braces. Some orphan FOs land at the merchant default location
  (when the Shopify product wasn't linked to Printify at order time).
- `read_assigned_fulfillment_orders`, `write_assigned_fulfillment_orders` — NOT needed.
  These are for apps that ARE fulfillment services themselves.

Symptom of missing third-party scope: the diagnostic prints `fulfillment_orders attached: 0`
even though the Shopify UI shows the order has line items waiting on fulfillment.

### Cause

**Cause.** When the user edits a Printify on-hold order via the UI to add a product
(because the Shopify product wasn't pre-linked when the order arrived), the added line
item has no `external.id` back-reference to the Shopify line. Printify ships everything
together with one tracking number, but only sends fulfillment notifications for line
items that DO have `external.id`. The Shopify order is left stuck in `partially_fulfilled`
with the orphan item showing "Fulfillment accepted" but no tracking — even though it
physically shipped in the same package.

**What the script does.** Per `--store`:
1. List Shopify orders with `fulfillment_status=partial` since `created_at_min` (default 7 days).
2. For each order, extract a single tracking number across existing fulfillments.
3. Get every open `fulfillment_order` on the order. (Strict location-matching was tried
   and abandoned — orphan FOs are typically pinned to the merchant's default location
   while shipped FOs are at Printify's location, so a strict location match would skip
   exactly the cases this tool exists to handle. The single-tracking-number check is
   the real safety bar.)
4. POST `/fulfillments.json` with the same tracking number applied to those orphan lines.

**Safety bar.** The script SKIPS (rather than guessing) when:
- The order has zero fulfillments yet (it's not actually a partial-fulfillment patch case).
- The order has multiple distinct tracking numbers across existing fulfillments.
- No open `fulfillment_order` exists on the order at all.

When an open FO's location differs from the shipped-from location, the dry-run shows a
`!` warning under that order so the user can spot it before applying.

These cases are rare and warrant manual handling — better to skip than to misfulfill.

**`--notify` flag.** Off by default. The customer already received the original tracking
email when the first fulfillment posted; the package physically contains everything anyway,
so re-emailing is usually noise. Pass `--notify` if you want the customer to get a second
email confirming the orphan items are on the same tracking.

**Decay.** As the proactive linker (`sync_products.py`) covers more of each store's
catalogue, the manual UI-edit workflow becomes rarer and this patcher's load drops to zero.

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
5. **Streamlit Cloud hosting** if remote access is wanted. Credentials would be pasted into Streamlit Cloud's secrets manager; `load_cfg(fallback=st.secrets)` already handles this — the central Dropbox path won't exist on Cloud, so it falls back automatically. The trade-off is that the Cloud copy becomes a second source of truth and will drift exactly as the old local copy did, so it needs a refresh step whenever a brand is added.

## Gotchas

- **Do not `@st.cache_resource` on `load_cfg()`.** We removed it so new stores in the central `secrets.toml` appear on the next rerun without restarting the Streamlit server. Re-adding the cache breaks that.
- **Multiselect selection persists — now auto-corrected, don't reintroduce the bug.** The Stores multiselect keeps its selection for the life of the browser session, so a `default=` only applies on first render; a store credentialed afterwards used to stay unticked and read as missing (this hid a store's orders from reconciliation for weeks, found 2026-08-17). `app.py` now keys the widget on `store_pick`, tracks `stores_seen`, and ticks any newly discovered store BEFORE the widget is instantiated. If you touch that block, keep the write ahead of the widget and don't re-add a `default=` alongside the `key=`.
- **A brand missing from the dashboard is almost never a code bug.** `ALL_STORES` is the intersection of the central file's `[shopify.*]` and `[printify.*]` keys, minus `EXCLUDED_STORES` — a brand with only one of the two sections is invisible. The sidebar now warns about half-wired brands and prints a credentialed-store count, so check those first. Then: both sections exist centrally → key spelt identically in both → not in `EXCLUDED_STORES` → **on the hosted Cloud app, the Cloud secrets copy is a separate source of truth and drifts** (see the Streamlit Cloud note in planned work).
- **Widget session_state rule.** You cannot assign to a widget's key AFTER the widget has been instantiated in the current Streamlit run. Only do widget-key assignments BEFORE the widget is re-instantiated on a subsequent run (see the "carry-over from previous send" block at the top of the per-store loop in `app.py`).
