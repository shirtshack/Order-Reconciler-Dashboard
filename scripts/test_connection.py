"""Connectivity test: verify Shopify + Printify credentials work for each configured store.

Shopify uses the client_credentials OAuth grant: we POST client_id + client_secret to
/admin/oauth/access_token and get a short-lived shpat_ token, which we then use against
/admin/api/{version}/shop.json to confirm the Admin API is reachable.

Run from the project root:
    python scripts/test_connection.py
"""
import sys
import tomllib
from pathlib import Path

import httpx

SECRETS = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"

if not SECRETS.exists():
    sys.exit(
        f"Missing {SECRETS}.\n"
        f"Copy .streamlit/secrets.toml.example to that location and fill in your credentials."
    )

cfg = tomllib.loads(SECRETS.read_text(encoding="utf-8"))

for key, shop in cfg.get("shopify", {}).items():
    print(f"\n[shopify.{key}]")
    r = httpx.post(
        f"https://{shop['store_domain']}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": shop["client_id"],
            "client_secret": shop["client_secret"],
        },
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  Token exchange FAIL {r.status_code}: {r.text[:300]}")
        continue
    token_resp = r.json()
    token = token_resp["access_token"]
    print(
        f"  Token OK — scope={token_resp.get('scope')!r} "
        f"expires_in={token_resp.get('expires_in')}s"
    )

    r = httpx.get(
        f"https://{shop['store_domain']}/admin/api/{shop['api_version']}/shop.json",
        headers={"X-Shopify-Access-Token": token},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  Shop API FAIL {r.status_code}: {r.text[:300]}")
    else:
        s = r.json()["shop"]
        print(f"  Shop OK — {s['name']} ({s['myshopify_domain']})")

for key, shop in cfg.get("printify", {}).items():
    print(f"\n[printify.{key}]")
    r = httpx.get(
        "https://api.printify.com/v1/shops.json",
        headers={"Authorization": f"Bearer {shop['access_token']}"},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  FAIL {r.status_code}: {r.text[:300]}")
        continue
    shops = r.json()
    print(f"  OK — {len(shops)} shop(s) on this Printify account:")
    for s in shops:
        marker = "  <-- configured" if str(s["id"]) == str(shop.get("shop_id", "")) else ""
        print(f"    id={s['id']}  title={s['title']}  sales_channel={s['sales_channel']}{marker}")
    if not shop.get("shop_id"):
        print("  -> Copy the correct shop id into secrets.toml under [printify.%s].shop_id" % key)
