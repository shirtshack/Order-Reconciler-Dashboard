"""Proactive linker: for a given Shopify vendor batch, create Printify products
that are pre-linked to the existing Shopify products.

Uses the POC-validated flow:
  1. POST /v1/shops/{shop}/uploads/images.json   (upload design PNG)
  2. POST /v1/shops/{shop}/products.json         (create product from template + new image)
  3. POST /v1/shops/{shop}/products/{id}/publishing_succeeded.json   (link to existing Shopify id)

NEVER deletes any product — deletion cascades to Shopify and destroys data.

Defaults to dry-run. Pass --execute to actually write.

    python scripts/sync_products.py --store divotclub --vendor "12-mar-t AU"
    python scripts/sync_products.py --store divotclub --vendor "12-mar-t AU" --execute
"""
import argparse
import base64
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from printify_send.clients.printify import PrintifyClient
from printify_send.clients.shopify import ShopifyClient

PRINTIFY_BASE = "https://api.printify.com/v1"

# Templates per country. Add more stores / combinations here as needed.
# (store_key, country) -> Printify product id (existing linked product to duplicate from)
TEMPLATES: dict[tuple[str, str], str] = {
    ("divotclub", "AU"): "69d75f576e732a90a806d127",  # yes retirement plan GOLF T-Shirt AU (bp=145, pp=34)
    ("divotclub", "US"): "69e1e8911c7619bbad06fef2",  # Einstein Teeing Off 6 T-Shirt US (bp=145, pp=29)
}

# Per-country primary-mockup color option id. Used to hint Printify to generate that
# color's mockup group first (so it becomes the "Primary" mockup by default).
# Ids come from the blueprint's Colors option values.
PRIMARY_COLOR_OPTION_ID: dict[str, int] = {
    "AU": 364,   # Military Green
    "US": 418,   # Black
}

PNG_ROOTS = [
    Path(r"Z:\001-600-sorted by mike PNG 2024\800 PNG"),
    Path(r"C:\001-600-sorted by mike PNG 2024\Divot Club Golf"),
]

# Title suffixes stripped to derive expected PNG filename stem
TITLE_SUFFIXES = (
    " T-Shirt AU", " T-Shirt US", " T-Shirt UK",
    " T Shirt AU", " T Shirt US", " T Shirt UK",
)


# ---------- helpers ----------

def strip_product_suffix(title: str) -> str:
    for suf in TITLE_SUFFIXES:
        if title.endswith(suf):
            return title[: -len(suf)].rstrip()
    return title


def detect_country(title: str) -> str | None:
    for suf, country in ((" AU", "AU"), (" US", "US"), (" UK", "UK")):
        if title.endswith(suf):
            return country
    return None


def build_png_index(roots: list[Path]) -> dict[str, Path]:
    """filename (case-normalised, no extension) -> preferred path.

    Prefers paths containing `\\art1\\` or `\\art 1\\` so we don't accidentally
    pick up a mockup JPG or upscaled variant.
    """
    idx: dict[str, Path] = {}
    count = 0
    for root in roots:
        if not root.exists():
            print(f"  skipping missing root: {root}")
            continue
        print(f"Indexing PNGs under {root}...")
        for p in root.rglob("*.png"):
            count += 1
            key = p.stem.strip().upper()
            path_lc = str(p).lower()
            is_art = "\\art1\\" in path_lc or "\\art 1\\" in path_lc
            existing_is_art = False
            if key in idx:
                existing_lc = str(idx[key]).lower()
                existing_is_art = "\\art1\\" in existing_lc or "\\art 1\\" in existing_lc
            if key not in idx or (is_art and not existing_is_art):
                idx[key] = p
    print(f"  indexed {count} PNG files ({len(idx)} unique stems).")
    return idx


def fetch_existing_printify_state(pr: PrintifyClient) -> tuple[set[str], set[str]]:
    """Return (set of linked Shopify ids, set of normalized existing Printify titles).

    Two ways a Shopify product may already be represented on Printify:
      1. Linked via API — external.id populated with the Shopify product id.
      2. Linked via Printify UI "Migrate product" — external.id is empty but a Printify
         product exists with the same normalised title.
    """
    linked_ids: set[str] = set()
    titles: set[str] = set()
    page = 1
    while True:
        r = httpx.get(
            f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products.json",
            headers=pr._headers(),
            params={"limit": 50, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        resp = r.json()
        for p in resp.get("data") or []:
            t = (p.get("title") or "").strip().upper()
            if t:
                titles.add(t)
            ext = p.get("external") or {}
            if ext.get("id"):
                linked_ids.add(str(ext["id"]))
        current = resp.get("current_page", page)
        last = resp.get("last_page", current)
        if current >= last:
            break
        page += 1
    return linked_ids, titles


def fetch_shopify_products(sh: ShopifyClient, vendor: str | None) -> list[dict]:
    """If vendor is given, fetch only that batch. Otherwise paginate the whole catalog."""
    out: list[dict] = []
    base_params: dict = {"limit": 250}
    if vendor:
        base_params["vendor"] = vendor
    url = f"https://{sh.store_domain}/admin/api/{sh.api_version}/products.json"
    # Parse Shopify's Link header for cursor-based pagination
    next_params: dict | None = dict(base_params)
    while next_params is not None:
        r = httpx.get(url, params=next_params, headers={"X-Shopify-Access-Token": sh._get_token()}, timeout=30)
        r.raise_for_status()
        out.extend(r.json().get("products") or [])
        # Shopify puts `<...&page_info=XYZ>; rel="next"` in the Link header
        link = r.headers.get("Link") or r.headers.get("link") or ""
        next_params = None
        for part in link.split(","):
            part = part.strip()
            if part.endswith('rel="next"'):
                # extract page_info from the URL
                import re
                m = re.search(r"page_info=([^&>]+)", part)
                if m:
                    next_params = {"limit": 250, "page_info": m.group(1)}
                break
        # small throttle to stay under Shopify's leaky bucket (2/sec refill, 40 burst)
        if next_params is not None:
            import time as _t
            _t.sleep(0.5)
    return out


def _upload_retry(pr: PrintifyClient, body: dict) -> dict:
    """Shared retry-on-5xx logic for upload."""
    import time as _t
    delay = 2.0
    last_response = None
    for _ in range(4):
        r = httpx.post(
            f"{PRINTIFY_BASE}/uploads/images.json",
            headers=pr._headers(),
            json=body,
            timeout=180,
        )
        last_response = r
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()
        _t.sleep(delay)
        delay *= 2
    last_response.raise_for_status()
    return {}  # unreachable


def upload_png(pr: PrintifyClient, png_path: Path) -> dict:
    """Upload a local PNG via base64. Bandwidth-heavy — prefer upload_png_from_url when possible."""
    contents = base64.b64encode(png_path.read_bytes()).decode("ascii")
    return _upload_retry(pr, {"file_name": png_path.name, "contents": contents})


def upload_png_from_url(pr: PrintifyClient, file_name: str, url: str) -> dict:
    """Tell Printify to fetch the PNG directly from a public URL. No local bandwidth used."""
    return _upload_retry(pr, {"file_name": file_name, "url": url})


def fetch_gcs_available_files(url_prefix: str) -> set[str] | None:
    """If url_prefix is a public GCS bucket URL, list the available filenames
    (basenames only, e.g. {'A girl who plays golf.png', ...}) via the JSON API.
    Returns None if the URL isn't GCS or listing fails — caller should skip the
    pre-check and let per-URL uploads 404 as needed.
    """
    parsed = urlparse(url_prefix)
    if parsed.netloc != "storage.googleapis.com":
        return None
    path = parsed.path.lstrip("/")
    if "/" not in path:
        return None
    bucket, prefix = path.split("/", 1)
    prefix = prefix.rstrip("/") + "/"  # ensure trailing slash for listing
    available: set[str] = set()
    page_token = None
    while True:
        params: dict = {"prefix": prefix, "maxResults": 1000}
        if page_token:
            params["pageToken"] = page_token
        r = httpx.get(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o",
            params=params, timeout=30,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        for item in data.get("items", []):
            name = item.get("name") or ""
            basename = name.split("/")[-1]
            if basename:
                available.add(basename)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return available


def fetch_template(pr: PrintifyClient, template_id: str) -> dict:
    r = httpx.get(
        f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{template_id}.json",
        headers=pr._headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def build_product_payload(
    template: dict,
    image_info: dict,
    new_title: str,
    print_area_width_px: int,
    print_area_height_px: int,
    primary_color_option_id: int | None = None,
    desired_ui_position_top: float = 0.02,
) -> dict:
    # Printify API's `y` is the image CENTER, as a fraction of print_area_height.
    # The UI's "Position top" shows the image TOP edge as a fraction of print_area_height.
    # Relationship: UI_position_top = y - (displayed_height / (2 * print_area_height))
    # Inverting: y = UI_position_top + displayed_height / (2 * print_area_height)
    #
    # API `scale` is displayed_width / print_area_width, so:
    #   displayed_width  = scale * print_area_width
    #   displayed_height = displayed_width * (source_height / source_width)
    #
    # `desired_ui_position_top` = 0 means image top at print box top.
    # 0.02 = image top at 2% below print box top (user's preference after testing).
    #
    # Neck / sleeve / back placeholders are deliberately EXCLUDED — the user doesn't
    # use them and Printify would stack design + default care label on the neck.
    PLACEMENT_X = 0.5
    ALLOWED_POSITIONS = {"front"}

    image_id = image_info["id"]
    src_w = int(image_info.get("width") or 0)
    src_h = int(image_info.get("height") or 0)

    print_areas = []
    for pa in template.get("print_areas") or []:
        new_pa = {"variant_ids": pa.get("variant_ids"), "placeholders": []}
        for ph in pa.get("placeholders") or []:
            position = ph.get("position")
            if position not in ALLOWED_POSITIONS:
                continue
            template_images = ph.get("images") or []
            if not template_images:
                continue
            scale = template_images[0].get("scale", 1)
            # Compute displayed image dimensions in print-area pixels
            if src_w and src_h and print_area_width_px and print_area_height_px:
                displayed_w = scale * print_area_width_px
                displayed_h = displayed_w * (src_h / src_w)
                half_h_frac = displayed_h / (2 * print_area_height_px)
                y = desired_ui_position_top + half_h_frac
            else:
                y = 0.35  # safe fallback — image top near top
            new_pa["placeholders"].append(
                {
                    "position": position,
                    "images": [
                        {
                            "id": image_id,
                            "x": PLACEMENT_X,
                            "y": y,
                            "scale": scale,
                            "angle": 0,
                        }
                    ],
                }
            )
        print_areas.append(new_pa)

    # Copy is_default from template. Then SORT variants so the priority color (Military
    # Green for AU, Black for US) comes first — Printify appears to generate mockup
    # groups in variant-order, so putting the priority color first makes it the primary mockup.
    template_variants = list(template.get("variants") or [])

    def variant_sort_key(v: dict) -> tuple:
        options = v.get("options") or []
        color_id = options[0] if options else 0
        is_priority = 0 if (primary_color_option_id and color_id == primary_color_option_id) else 1
        return (is_priority, int(v.get("id", 0)))

    template_variants.sort(key=variant_sort_key)

    variants = [
        {
            "id": v["id"],
            "price": v["price"],
            "is_enabled": v.get("is_enabled", True),
            "is_default": v.get("is_default", False),
        }
        for v in template_variants
    ]

    return {
        "title": new_title,
        "description": template.get("description") or "",
        "blueprint_id": template["blueprint_id"],
        "print_provider_id": template["print_provider_id"],
        "variants": variants,
        "print_areas": print_areas,
    }


def create_printify_product(pr: PrintifyClient, payload: dict) -> str:
    """Create product with retry on 5xx (same reason as upload_png)."""
    delay = 2.0
    last_response = None
    for attempt in range(4):
        r = httpx.post(
            f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products.json",
            headers=pr._headers(),
            json=payload,
            timeout=60,
        )
        last_response = r
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()["id"]
        import time as _t
        _t.sleep(delay)
        delay *= 2
    last_response.raise_for_status()
    return ""


def link_to_shopify(pr: PrintifyClient, printify_id: str, sh: ShopifyClient, shopify_prod: dict) -> None:
    handle_url = f"https://{sh.store_domain}/products/{shopify_prod['handle']}"
    r = httpx.post(
        f"{PRINTIFY_BASE}/shops/{pr.shop_id}/products/{printify_id}/publishing_succeeded.json",
        headers=pr._headers(),
        json={"external": {"id": str(shopify_prod["id"]), "handle": handle_url}},
        timeout=60,
    )
    r.raise_for_status()


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="Store key in secrets.toml")
    ap.add_argument("--vendor", default=None, help='Shopify vendor to sync (optional). If omitted, processes the full Shopify catalogue.')
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually create products. Default is dry-run (no Printify writes).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N products (useful for testing one before blasting through a batch).",
    )
    ap.add_argument(
        "--skip-linking",
        action="store_true",
        help="Create Printify products as pure drafts without calling publishing_succeeded. "
             "User then manually migrates each via Printify UI. Leaves Shopify fully untouched.",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Number of parallel product creations (default 5). Higher = faster but risks rate limits.",
    )
    ap.add_argument(
        "--image-url-prefix",
        default=None,
        help='Public URL prefix where PNGs live (e.g. "https://storage.googleapis.com/bucket/Golf Art batch 2"). '
             'If set, Printify fetches each PNG directly from this URL (filename = "<title-minus-suffix>.png", URL-encoded). '
             'Bypasses local PNG index and outbound bandwidth.',
    )
    args = ap.parse_args()
    dry = not args.execute

    cfg = tomllib.loads((ROOT / ".streamlit" / "secrets.toml").read_text(encoding="utf-8"))
    sh = ShopifyClient(cfg["shopify"][args.store])
    pr = PrintifyClient(cfg["printify"][args.store])

    mode = "DRY RUN (no writes)" if dry else "!! EXECUTE MODE - will create Printify products !!"
    scope = f"vendor {args.vendor!r}" if args.vendor else "ENTIRE CATALOGUE"
    print("=" * 70)
    print(f"Store: {args.store}  |  Scope: {scope}  |  {mode}")
    print("=" * 70)

    # 1. Find Shopify products (one vendor or whole catalogue)
    print("\n[1/4] Fetching Shopify products...")
    shopify_products = fetch_shopify_products(sh, args.vendor)
    print(f"  {len(shopify_products)} product(s) fetched")
    if not shopify_products:
        print("  Nothing to do.")
        return 0

    # 2. Figure out what's already represented on Printify
    print("\n[2/4] Fetching existing Printify products to detect already-linked / already-migrated...")
    linked_ids, printify_titles = fetch_existing_printify_state(pr)
    print(f"  {len(linked_ids)} Shopify id(s) linked via external.id on Printify")
    print(f"  {len(printify_titles)} distinct Printify product title(s) already exist")

    # 3. Build PNG index (only needed when uploading local files)
    print()
    gcs_available: set[str] | None = None
    if args.image_url_prefix:
        print(f"URL mode — skipping local PNG index. Prefix: {args.image_url_prefix}")
        png_index = {}
        gcs_available = fetch_gcs_available_files(args.image_url_prefix)
        if gcs_available is not None:
            print(f"  GCS bucket listing: {len(gcs_available)} PNG(s) available for pre-check")
        else:
            print("  (bucket listing unavailable or not a GCS URL — will let individual uploads 404 if missing)")
    else:
        png_index = build_png_index(PNG_ROOTS)

    # 4. For each Shopify product, decide what to do
    print(f"\n[3/4] Planning work for {len(shopify_products)} product(s)...")
    plans: list[dict] = []
    skipped_already_linked = 0
    skipped_already_migrated = 0
    skipped_no_png = []
    skipped_no_country = []
    skipped_no_template = []
    for sp in shopify_products:
        sid = str(sp["id"])
        title = sp["title"]
        title_norm = (title or "").strip().upper()
        if sid in linked_ids:
            skipped_already_linked += 1
            continue
        if title_norm in printify_titles:
            skipped_already_migrated += 1
            continue
        country = detect_country(title)
        if country is None:
            skipped_no_country.append(title)
            continue
        template_id = TEMPLATES.get((args.store, country))
        if not template_id:
            skipped_no_template.append((title, country))
            continue
        stem = strip_product_suffix(title).strip()
        filename = f"{stem}.png"
        plan_entry = {
            "shopify_id": sid,
            "shopify_title": title,
            "shopify_product": sp,
            "country": country,
            "template_id": template_id,
            "png_filename": filename,
        }
        if args.image_url_prefix:
            # If we could list the bucket, pre-check availability
            if gcs_available is not None and filename not in gcs_available:
                skipped_no_png.append((title, filename))
                continue
            # Encode the FULL path (prefix + filename). The prefix may contain spaces
            # that Printify's URL validator rejects if mixed with %20 in the filename.
            parsed = urlparse(args.image_url_prefix.rstrip("/"))
            full_path = f"{parsed.path}/{filename}"
            plan_entry["png_url"] = f"{parsed.scheme}://{parsed.netloc}{quote(full_path, safe='/')}"
            plan_entry["png_path"] = None
        else:
            stem_upper = stem.upper()
            png_path = png_index.get(stem_upper)
            if png_path is None:
                skipped_no_png.append((title, stem_upper))
                continue
            plan_entry["png_path"] = png_path
            plan_entry["png_url"] = None
        plans.append(plan_entry)

    if args.limit is not None and len(plans) > args.limit:
        print(f"  (limiting to first {args.limit} of {len(plans)} planned)")
        plans = plans[: args.limit]

    print(f"  {len(plans)} to create")
    print(f"  {skipped_already_linked} skipped (already linked via external.id)")
    print(f"  {skipped_already_migrated} skipped (Printify already has a product with this title)")
    print(f"  {len(skipped_no_png)} skipped (PNG not found)")
    print(f"  {len(skipped_no_country)} skipped (no AU/US/UK suffix in title)")
    print(f"  {len(skipped_no_template)} skipped (no template for country — e.g. UK goes to Merch Panda)")

    if skipped_no_png:
        print("\n  PNGs that could not be found (check filename/location):")
        for t, stem in skipped_no_png[:20]:
            print(f"    - {t}  (looked for {stem}.png)")
        if len(skipped_no_png) > 20:
            print(f"    ... and {len(skipped_no_png) - 20} more")

    if skipped_no_country:
        print("\n  Titles where country couldn't be detected:")
        for t in skipped_no_country[:10]:
            print(f"    - {t}")

    if skipped_no_template:
        print("\n  Countries with no configured template:")
        for t, c in skipped_no_template[:10]:
            print(f"    - {t}  (country={c})")

    if dry:
        print(f"\n[4/4] DRY RUN — the following {len(plans)} product(s) WOULD be created:")
        for plan in plans:
            src = plan["png_url"] if plan.get("png_url") else plan["png_path"]
            print(f"  + {plan['shopify_title']}  (country={plan['country']}, png={src})")
        print("\nRe-run with --execute to actually create them.")
        return 0

    # EXECUTE
    print(f"\n[4/4] EXECUTE: creating {len(plans)} product(s) with {args.workers} worker(s)...")

    # Pre-fetch every template used by this batch — avoids races in parallel workers.
    template_cache: dict[str, dict] = {}
    for tpl_id in {p["template_id"] for p in plans}:
        print(f"  pre-fetching template {tpl_id}...")
        template_cache[tpl_id] = fetch_template(pr, tpl_id)

    pa_w, pa_h = 4682, 5291  # Gildan 64000 print area

    print_lock = threading.Lock()
    def log(msg: str) -> None:
        with print_lock:
            print(msg, flush=True)

    def process_one(i: int, plan: dict) -> tuple[str | None, str | None]:
        title = plan["shopify_title"]
        try:
            template = template_cache[plan["template_id"]]
            primary_color_id = PRIMARY_COLOR_OPTION_ID.get(plan["country"])
            if plan.get("png_url"):
                image_info = upload_png_from_url(pr, plan["png_filename"], plan["png_url"])
            else:
                image_info = upload_png(pr, plan["png_path"])
            payload = build_product_payload(
                template, image_info, title, pa_w, pa_h,
                primary_color_option_id=primary_color_id,
            )
            new_id = create_printify_product(pr, payload)
            if not args.skip_linking:
                link_to_shopify(pr, new_id, sh, plan["shopify_product"])
            log(f"  [{i}/{len(plans)}] OK  {title}")
            return (title, None)
        except Exception as e:  # API boundary — don't kill the batch
            err = f"{type(e).__name__}: {e}"
            if isinstance(e, httpx.HTTPStatusError):
                err += f"  [body: {e.response.text[:300]}]"
            log(f"  [{i}/{len(plans)}] FAIL  {title} - {err}")
            return (None, f"{title}: {err}")

    successes: list[str] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process_one, i, plan) for i, plan in enumerate(plans, start=1)]
        for fut in as_completed(futures):
            succ, fail = fut.result()
            if succ:
                successes.append(succ)
            if fail:
                failures.append(fail)

    # Summary
    print("\n" + "=" * 70)
    print(f"Done. {len(successes)} created, {len(failures)} failed.")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f}")
    print("=" * 70)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
