from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import discord

from .anonpay import AnonpayClient, AnonpayError
from .config import Settings
from .database import Database, utc_now

FINAL_STATUSES = {"finished", "paid partially", "failed", "expired", "halted", "refunded"}


@dataclass(slots=True)
class PaymentSession:
    id: int
    guild_id: int
    user_id: int
    profile_id: int
    anonpay_id: str
    status: str
    status_url: str
    checkout_url: str
    webhook_url: Optional[str]
    expires_at: datetime
    last_payload: Optional[dict[str, Any]]


class PaymentManager:
    """Coordinates interactions between Discord, the database, and AnonPay."""

    def __init__(self, settings: Settings, db: Database, anonpay: AnonpayClient) -> None:
        self._settings = settings
        self._db = db
        self._anonpay = anonpay

    async def ensure_guild_setup(self, guild_id: int) -> Optional[dict[str, Any]]:
        return await self._db.get_guild_settings(guild_id)

    async def setup_guild(self, guild_id: int, address: str, coin: str, network: str) -> None:
        await self._db.set_guild_settings(
            guild_id=guild_id,
            payout_address=address,
            ticker_to=coin.upper(),
            network_to=network.upper(),
        )

    async def create_payment_profile(
        self,
        guild_id: int,
        name: str,
        role: Optional[discord.Role],
        duration_days: Optional[int],
        parameters: dict[str, Any],
    ) -> int:
        donation_mode = bool(parameters.get("donation", False))
        if role:
            parameters["discord_role_id"] = role.id
        if duration_days:
            parameters["duration_days"] = duration_days
        return await self._db.create_payment_profile(
            guild_id=guild_id,
            name=name,
            role_id=role.id if role else None,
            duration_days=duration_days,
            parameters=parameters,
            donation_mode=donation_mode,
        )

    async def delete_payment_profile(self, guild_id: int, name: str) -> Optional[int]:
        return await self._db.delete_payment_profile(guild_id, name)

    async def list_payment_profiles(self, guild_id: int) -> list[dict[str, Any]]:
        return await self._db.list_payment_profiles(guild_id)

    async def get_payment_profile(self, guild_id: int, name: str) -> Optional[dict[str, Any]]:
        return await self._db.get_payment_profile(guild_id, name)

    async def start_payment_session(
        self,
        guild_id: int,
        user_id: int,
        profile: dict[str, Any],
        guild_settings: dict[str, Any],
    ) -> tuple[PaymentSession, dict[str, Any]]:
        params = {
            "direct": "false",
            "address": guild_settings["payout_address"],
            "ticker_to": guild_settings["ticker_to"],
            "network_to": guild_settings["network_to"],
        }
        for key, value in profile["parameters"].items():
            if key in {"discord_role_id", "duration_days"}:
                continue
            if value is None:
                continue
            params[key] = str(value).lower() if isinstance(value, bool) else value

        response = await self._anonpay.create_checkout(params)

        anonpay_id = str(response.get("id"))
        if not anonpay_id:
            raise AnonpayError("AnonPay response missing identifier.")

        status_url = response.get("status_url")
        url = response.get("url")
        if not status_url or not url:
            raise AnonpayError("AnonPay response missing required URLs.")

        expires_at = utc_now() + timedelta(minutes=self._settings.session_ttl_minutes)
        webhook_url = profile["parameters"].get("webhook")

        session_id = await self._db.create_payment_session(
            guild_id=guild_id,
            user_id=user_id,
            payment_profile_id=int(profile["id"]),
            anonpay_id=anonpay_id,
            status=str(response.get("status", "waiting")),
            status_url=status_url,
            checkout_url=url,
            webhook_url=webhook_url,
            expires_at=expires_at,
            payload=response,
        )

        session = PaymentSession(
            id=session_id,
            guild_id=guild_id,
            user_id=user_id,
            profile_id=int(profile["id"]),
            anonpay_id=anonpay_id,
            status=str(response.get("status", "waiting")),
            status_url=status_url,
            checkout_url=url,
            webhook_url=webhook_url,
            expires_at=expires_at,
            last_payload=response,
        )
        return session, response

    async def load_active_sessions(self) -> list[PaymentSession]:
        cutoff = utc_now() - timedelta(minutes=self._settings.session_ttl_minutes * 2)
        rows = await self._db.list_active_sessions(cutoff)
        sessions: list[PaymentSession] = []
        for row in rows:
            sessions.append(
                PaymentSession(
                    id=row["id"],
                    guild_id=row["guild_id"],
                    user_id=row["user_id"],
                    profile_id=row["payment_profile_id"],
                    anonpay_id=row["anonpay_id"],
                    status=row["status"],
                    status_url=row["status_url"],
                    checkout_url=row["checkout_url"],
                    webhook_url=row["webhook_url"],
                    expires_at=row["expires_at"],
                    last_payload=row["last_payload"],
                )
            )
        return sessions

    async def refresh_session_status(self, session: PaymentSession) -> Optional[dict[str, Any]]:
        payload = await self._anonpay.fetch_status(session.status_url)
        status = str(payload.get("status", session.status)).lower()
        await self._db.update_payment_session_status(session.id, status, payload)
        session.status = status
        session.last_payload = payload
        return payload

    async def purge_session(self, session_id: int) -> None:
        await self._db.delete_payment_session(session_id)

    async def upsert_subscription(
        self,
        session: PaymentSession,
        profile: dict[str, Any],
        webhook_url: Optional[str],
    ) -> datetime:
        duration_days = profile.get("duration_days")
        if not duration_days:
            raise RuntimeError("Cannot create subscription without a configured duration.")
        expires_at = utc_now() + timedelta(days=int(duration_days))
        await self._db.upsert_subscription(
            guild_id=session.guild_id,
            user_id=session.user_id,
            payment_profile_id=session.profile_id,
            role_id=profile.get("role_id"),
            expires_at=expires_at,
            webhook_url=webhook_url,
        )
        return expires_at

    async def get_payment_profile_by_id(self, profile_id: int) -> Optional[dict[str, Any]]:
        return await self._db.get_payment_profile_by_id(profile_id)

    async def list_expiring_subscriptions(self, reference_time: datetime, days_ahead: int) -> list[dict[str, Any]]:
        return await self._db.list_expiring_subscriptions(reference_time, days_ahead)

    async def mark_subscription_notified(self, subscription_id: int) -> None:
        await self._db.mark_subscription_notified(subscription_id)

    async def list_expired_subscriptions(self, reference_time: datetime) -> list[dict[str, Any]]:
        return await self._db.list_expired_subscriptions(reference_time)

    async def delete_subscription(self, subscription_id: int) -> None:
        await self._db.delete_subscription(subscription_id)

    async def list_subscriptions_for_profile(self, profile_id: int) -> list[dict[str, Any]]:
        return await self._db.list_subscriptions_for_profile(profile_id)
