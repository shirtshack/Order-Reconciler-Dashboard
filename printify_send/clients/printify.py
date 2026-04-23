"""Printify v1 API client."""
from typing import Iterator

import httpx

BASE = "https://api.printify.com/v1"


class PrintifyClient:
    def __init__(self, cfg: dict):
        self.token = cfg["access_token"]
        self.shop_id = cfg["shop_id"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def list_orders(self, status: str | None = None, limit: int = 50, page: int = 1) -> dict:
        params: dict = {"limit": limit, "page": page}
        if status:
            params["status"] = status
        r = httpx.get(
            f"{BASE}/shops/{self.shop_id}/orders.json",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def iter_orders(self, status: str | None = None) -> Iterator[dict]:
        """Walk every page. Also filters client-side in case the server ignores `status`."""
        page = 1
        while True:
            resp = self.list_orders(status=status, limit=50, page=page)
            data = resp.get("data", []) or []
            if not data:
                return
            for order in data:
                if status and order.get("status") != status:
                    continue
                yield order
            current = resp.get("current_page", page)
            last = resp.get("last_page", current)
            if current >= last:
                return
            page += 1

    def get_order(self, order_id: str) -> dict:
        r = httpx.get(
            f"{BASE}/shops/{self.shop_id}/orders/{order_id}.json",
            headers=self._headers(),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def send_to_production(self, order_id: str) -> dict:
        """Send an on-hold order to production. Printify returns the updated order."""
        r = httpx.post(
            f"{BASE}/shops/{self.shop_id}/orders/{order_id}/send_to_production.json",
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
