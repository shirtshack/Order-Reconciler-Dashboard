"""Shopify Admin API client.

Uses the client_credentials OAuth grant (custom-distribution app) — posts client_id +
client_secret to /admin/oauth/access_token and caches the returned shpat_ token until
shortly before its expires_in deadline.
"""
import time

import httpx

TOKEN_REFRESH_BUFFER_SEC = 60  # refresh at least a minute before Shopify-reported expiry


class ShopifyClient:
    def __init__(self, cfg: dict):
        self.store_domain = cfg["store_domain"]
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.api_version = cfg["api_version"]
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_BUFFER_SEC:
            return self._token
        r = httpx.post(
            f"https://{self.store_domain}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=10,
        )
        r.raise_for_status()
        resp = r.json()
        self._token = resp["access_token"]
        self._token_expires_at = time.time() + int(resp.get("expires_in", 86399))
        return self._token

    def _headers(self) -> dict:
        return {"X-Shopify-Access-Token": self._get_token()}

    def get_order(self, order_id: str | int) -> dict | None:
        """Fetch a Shopify order by its numeric id. Returns None if 404.

        Retries on 429 using Shopify's Retry-After hint.
        """
        url = f"https://{self.store_domain}/admin/api/{self.api_version}/orders/{order_id}.json"
        for _ in range(5):
            r = httpx.get(url, headers=self._headers(), timeout=10)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "2")))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()["order"]
        r.raise_for_status()
        return None

    def get_orders(self, order_ids: list[str | int]) -> dict[str, dict]:
        """Bulk-fetch orders by id. Returns {str(id): order}. Missing ids just aren't in the dict.

        Shopify's /orders.json accepts up to 250 ids via the `ids` query param. We chunk
        larger lists and merge. 429s are retried per chunk.
        """
        if not order_ids:
            return {}
        url = f"https://{self.store_domain}/admin/api/{self.api_version}/orders.json"
        out: dict[str, dict] = {}
        for i in range(0, len(order_ids), 250):
            chunk = order_ids[i : i + 250]
            params = {
                "ids": ",".join(str(x) for x in chunk),
                "status": "any",  # include closed/archived
                "limit": 250,
            }
            for _ in range(5):
                r = httpx.get(url, headers=self._headers(), params=params, timeout=30)
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", "2")))
                    continue
                r.raise_for_status()
                for order in r.json().get("orders", []):
                    out[str(order["id"])] = order
                break
            else:
                r.raise_for_status()
        return out
