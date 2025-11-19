from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp


class AnonpayError(RuntimeError):
    """Generic error wrapper for AnonPay API interactions."""


log = logging.getLogger(__name__)


class AnonpayClient:
    """HTTP client for interacting with Trocador's AnonPay endpoints."""

    BASE_URL = "https://trocador.app/anonpay"

    def __init__(self, timeout: int = 30, user_agent: str = "CatPaymentBot/1.0") -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_agent = user_agent

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        self._session = aiohttp.ClientSession(timeout=self._timeout, headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def create_checkout(self, params: dict[str, Any]) -> dict[str, Any]:
        if "direct" not in params:
            params["direct"] = "false"

        session = await self._get_session()
        try:
            async with session.get(self.BASE_URL, params=params) as response:
                body = await response.text()
                if response.status >= 400:
                    raise AnonpayError(f"AnonPay returned HTTP {response.status}: {body}")
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as exc:
                    self._log_non_json_payload(
                        action="creating checkout",
                        status=response.status,
                        content_type=response.headers.get("Content-Type"),
                        body=body,
                    )
                    raise AnonpayError("AnonPay returned an unexpected payload while creating checkout.") from exc
        except asyncio.TimeoutError as exc:
            raise AnonpayError("Timed out while creating AnonPay checkout.") from exc
        except aiohttp.ClientError as exc:
            raise AnonpayError("Network error while creating AnonPay checkout.") from exc
        return data

    async def fetch_status(self, status_url: str) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(status_url) as response:
                body = await response.text()
                if response.status >= 400:
                    raise AnonpayError(f"AnonPay status call failed with {response.status}: {body}")
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    self._log_non_json_payload(
                        action="fetching status",
                        status=response.status,
                        content_type=response.headers.get("Content-Type"),
                        body=body,
                    )
                    raise AnonpayError("AnonPay returned an unexpected payload while fetching status.") from exc
        except asyncio.TimeoutError as exc:
            raise AnonpayError("Timed out while fetching AnonPay status.") from exc
        except aiohttp.ClientError as exc:
            raise AnonpayError("Network error while fetching AnonPay status.") from exc

    async def fetch_text(self, url: str, params: Optional[dict[str, Any]] = None) -> str:
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise AnonpayError(f"HTTP {response.status} while fetching {url}: {body}")
                return await response.text()
        except asyncio.TimeoutError as exc:
            raise AnonpayError("Timed out while fetching data from AnonPay.") from exc
        except aiohttp.ClientError as exc:
            raise AnonpayError("Network error while fetching data from AnonPay.") from exc

    async def post_webhook(self, url: str, payload: dict[str, Any]) -> None:
        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise AnonpayError(f"Webhook POST failed with {response.status}: {body}")
        except asyncio.TimeoutError as exc:
            raise AnonpayError("Timed out while posting webhook update.") from exc
        except aiohttp.ClientError as exc:
            raise AnonpayError("Network error while posting webhook update.") from exc

    @staticmethod
    def _log_non_json_payload(
        *,
        action: str,
        status: int,
        content_type: Optional[str],
        body: str,
    ) -> None:
        snippet = body.strip()
        if not snippet:
            snippet = "<empty body>"
        elif len(snippet) > 500:
            snippet = f"{snippet[:500]}…"
        log.error(
            "AnonPay returned non-JSON payload while %s (status=%s, content-type=%s): %s",
            action,
            status,
            content_type,
            snippet,
        )
