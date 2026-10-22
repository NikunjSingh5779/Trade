"""Paper-only execution client for Alpaca's Trading API.

The endpoint is intentionally hard-coded to Alpaca's paper host. This module has no
configuration path to the live trading host.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class AlpacaPaperError(RuntimeError):
    """An error returned by the Alpaca paper Trading API."""


class AlpacaOrderOutcomeUnknown(AlpacaPaperError):
    """A write may have reached Alpaca but its final outcome was not confirmed."""


class AlpacaOrderRejected(AlpacaPaperError):
    """Alpaca definitively rejected or canceled the order."""


class AlpacaPaperTrading:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_key = settings.alpaca_paper_api_key.strip()
        self.api_secret = settings.alpaca_paper_api_secret.strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.alpaca_paper_trading_enabled)

    def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None, missing_ok: bool = False,
    ) -> Any:
        if not self.configured:
            raise AlpacaPaperError(
                "Alpaca Paper execution is enabled but its paper API credentials are missing. "
                "Set ALPACA_PAPER_API_KEY and ALPACA_PAPER_API_SECRET in the local .env file."
            )
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=12.0) as client:
                response = client.request(
                    method, f"{PAPER_BASE_URL}{path}", headers=headers,
                    params=params, json=payload,
                )
        except httpx.HTTPError as exc:
            error = f"Alpaca Paper API connection failed ({type(exc).__name__})."
            exception = AlpacaOrderOutcomeUnknown if method.upper() in {"POST", "DELETE"} else AlpacaPaperError
            raise exception(error) from exc
        if missing_ok and response.status_code == 404:
            return None
        if response.is_error:
            if method.upper() in {"POST", "DELETE"} and response.status_code >= 500:
                raise AlpacaOrderOutcomeUnknown(
                    f"Alpaca Paper returned HTTP {response.status_code} to a write request; "
                    "the result may be ambiguous. Reconcile by client order ID before retrying."
                )
            if method.upper() == "POST" and response.status_code in {400, 409, 422}:
                try:
                    failed_body = response.json()
                    reason = failed_body.get("message") or failed_body.get("code") or "order rejected"
                except (ValueError, AttributeError):
                    reason = "order rejected"
                raise AlpacaOrderRejected(
                    f"Alpaca Paper rejected the order (HTTP {response.status_code}): {str(reason)[:240]}"
                )
            try:
                body = response.json()
                detail = body.get("message") or body.get("code") or "request rejected"
            except (ValueError, AttributeError):
                detail = "request rejected"
            request_id = response.headers.get("X-Request-ID")
            suffix = f" (request ID {request_id})" if request_id else ""
            raise AlpacaPaperError(
                f"Alpaca Paper API returned HTTP {response.status_code}: {str(detail)[:240]}{suffix}"
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaPaperError("Alpaca Paper API returned an invalid JSON response.") from exc

    def account(self) -> dict[str, Any]:
        body = self._request("GET", "/v2/account")
        if not isinstance(body, dict):
            raise AlpacaPaperError("Alpaca Paper account response was not an object.")
        return body

    def positions(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/v2/positions")
        if not isinstance(body, list):
            raise AlpacaPaperError("Alpaca Paper positions response was not a list.")
        return body

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        body = self._request(
            "GET", "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id, "nested": "true"},
            missing_ok=True,
        )
        return body if isinstance(body, dict) else None

    def order_by_id(self, order_id: str) -> dict[str, Any]:
        body = self._request(
            "GET", f"/v2/orders/{quote(order_id, safe='')}", params={"nested": "true"}
        )
        if not isinstance(body, dict):
            raise AlpacaPaperError("Alpaca Paper order response was not an object.")
        return body

    def _wait_for_fill(self, order: dict[str, Any], attempts: int = 20) -> dict[str, Any]:
        order_id = str(order.get("id", "")).strip()
        if not order_id:
            raise AlpacaOrderOutcomeUnknown(
                "Alpaca Paper may have accepted the order but returned no order ID. "
                "Reconcile by client order ID before retrying."
            )
        latest = order
        for attempt in range(attempts):
            status = str(latest.get("status", "")).lower()
            if status == "filled":
                if not latest.get("filled_avg_price"):
                    latest = self.order_by_id(order_id)
                    if latest.get("status") == "filled" and latest.get("filled_avg_price"):
                        return latest
                    raise AlpacaPaperError("Alpaca Paper order is filled but has no fill price.")
                return latest
            if status in {"rejected", "canceled", "expired", "replaced"}:
                message = latest.get("reject_reason") or status
                raise AlpacaOrderRejected(
                    f"Alpaca Paper order did not fill: {str(message)[:200]}."
                )
            time.sleep(0.5)
            latest = self.order_by_id(order_id)
        raise AlpacaOrderOutcomeUnknown(
            f"Alpaca Paper order {order_id} is not confirmed filled yet. Do not submit a new order; "
            "the app will reconcile this client order ID before allowing another entry."
        )

    @staticmethod
    def _price(value: float) -> str:
        # Alpaca allows two decimals at/above $1 and four below $1 for equity stop/limit prices.
        return f"{value:.2f}" if value >= 1 else f"{value:.4f}"

    def submit_bracket(
        self, *, symbol: str, side: str, quantity: int, stop_loss: float,
        take_profit: float, client_order_id: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise AlpacaPaperError("Alpaca Paper execution is not enabled.")
        if quantity < 1:
            raise AlpacaPaperError("Alpaca Paper order quantity must be at least one share.")
        normalized_side = "buy" if side == "long" else "sell"
        existing = self.order_by_client_id(client_order_id)
        if existing is None:
            order = self._request("POST", "/v2/orders", payload={
                "symbol": symbol.upper(),
                "qty": str(int(quantity)),
                "side": normalized_side,
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "client_order_id": client_order_id,
                "take_profit": {"limit_price": self._price(float(take_profit))},
                "stop_loss": {"stop_price": self._price(float(stop_loss))},
            })
        else:
            # Re-use an order after a timeout/restart; never submit a duplicate entry.
            order = existing
            if int(float(order.get("qty", quantity))) != int(quantity):
                raise AlpacaPaperError(
                    "An Alpaca Paper order already exists for this proposal with a different quantity; "
                    "reconcile it in Alpaca before retrying."
                )
        if not isinstance(order, dict):
            raise AlpacaPaperError("Alpaca Paper did not return an order object.")
        return self._wait_for_fill(order)

    def close_position(self, symbol: str) -> dict[str, Any]:
        if not self.enabled:
            raise AlpacaPaperError("Alpaca Paper execution is not enabled.")
        response = self._request(
            "DELETE", f"/v2/positions/{quote(symbol.upper(), safe='')}"
        )
        if not isinstance(response, dict):
            raise AlpacaPaperError("Alpaca Paper close request returned no order.")
        order_id = str(response.get("id", ""))
        if not order_id:
            raise AlpacaOrderOutcomeUnknown(
                "Alpaca may have accepted the close order but returned no order ID; "
                "reconcile the account before trying to close again."
            )
        return self._wait_for_fill(response)
