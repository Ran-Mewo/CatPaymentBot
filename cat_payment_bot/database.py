from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    """Simple async wrapper around aiosqlite with convenience helpers."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        if self._conn:
            return
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA temp_store = MEMORY")
        await self._conn.commit()
        await self._create_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_schema(self) -> None:
        assert self._conn is not None
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                payout_address TEXT NOT NULL,
                ticker_to TEXT NOT NULL,
                network_to TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                role_id INTEGER,
                duration_days INTEGER,
                parameters TEXT NOT NULL,
                donation_mode INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, name),
                FOREIGN KEY(guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_profiles_guild_id_name_nocase
            ON payment_profiles (guild_id, name COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS payment_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                payment_profile_id INTEGER NOT NULL,
                anonpay_id TEXT NOT NULL,
                status TEXT NOT NULL,
                status_url TEXT NOT NULL,
                checkout_url TEXT NOT NULL,
                webhook_url TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_status_check TEXT,
                last_status TEXT,
                last_payload TEXT,
                FOREIGN KEY(payment_profile_id) REFERENCES payment_profiles(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                payment_profile_id INTEGER NOT NULL,
                role_id INTEGER,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_notified_at TEXT,
                webhook_url TEXT,
                UNIQUE(guild_id, user_id, payment_profile_id),
                FOREIGN KEY(payment_profile_id) REFERENCES payment_profiles(id) ON DELETE CASCADE
            );
            """
        )
        await self._conn.commit()

    async def execute(self, query: str, parameters: Sequence[Any] = ()) -> None:
        assert self._conn is not None
        await self._conn.execute(query, parameters)
        await self._conn.commit()

    async def fetch_one(self, query: str, parameters: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
        assert self._conn is not None
        self._conn.row_factory = aiosqlite.Row
        async with self._conn.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, query: str, parameters: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        assert self._conn is not None
        self._conn.row_factory = aiosqlite.Row
        async with self._conn.execute(query, parameters) as cursor:
            return await cursor.fetchall()

    async def set_guild_settings(self, guild_id: int, payout_address: str, ticker_to: str, network_to: str) -> None:
        now = utc_now().isoformat()
        await self.execute(
            """
            INSERT INTO guild_settings (guild_id, payout_address, ticker_to, network_to, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                payout_address = excluded.payout_address,
                ticker_to = excluded.ticker_to,
                network_to = excluded.network_to,
                updated_at = excluded.updated_at
            """,
            (guild_id, payout_address, ticker_to, network_to, now, now),
        )

    async def get_guild_settings(self, guild_id: int) -> Optional[dict[str, Any]]:
        row = await self.fetch_one(
            "SELECT guild_id, payout_address, ticker_to, network_to FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        if row is None:
            return None
        return dict(row)

    async def create_payment_profile(
        self,
        guild_id: int,
        name: str,
        role_id: Optional[int],
        duration_days: Optional[int],
        parameters: dict[str, Any],
        donation_mode: bool,
    ) -> int:
        now = utc_now().isoformat()
        await self.execute(
            """
            INSERT INTO payment_profiles (guild_id, name, role_id, duration_days, parameters, donation_mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                name,
                role_id,
                duration_days,
                json.dumps(parameters),
                int(donation_mode),
                now,
                now,
            ),
        )
        row = await self.fetch_one(
            "SELECT id FROM payment_profiles WHERE guild_id = ? AND name = ? COLLATE NOCASE",
            (guild_id, name),
        )
        assert row is not None
        return int(row["id"])

    async def get_payment_profile(self, guild_id: int, name: str) -> Optional[dict[str, Any]]:
        row = await self.fetch_one(
            """
            SELECT id, guild_id, name, role_id, duration_days, parameters, donation_mode
            FROM payment_profiles
            WHERE guild_id = ? AND name = ? COLLATE NOCASE
            """,
            (guild_id, name),
        )
        if row is None:
            return None
        data = dict(row)
        data["parameters"] = json.loads(data["parameters"])
        data["donation_mode"] = bool(data["donation_mode"])
        return data

    async def list_payment_profiles(self, guild_id: int) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            """
            SELECT id, guild_id, name, role_id, duration_days, parameters, donation_mode
            FROM payment_profiles
            WHERE guild_id = ?
            ORDER BY name COLLATE NOCASE ASC
            """,
            (guild_id,),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["parameters"] = json.loads(payload["parameters"])
            payload["donation_mode"] = bool(payload["donation_mode"])
            results.append(payload)
        return results

    async def get_payment_profile_by_id(self, profile_id: int) -> Optional[dict[str, Any]]:
        row = await self.fetch_one(
            """
            SELECT id, guild_id, name, role_id, duration_days, parameters, donation_mode
            FROM payment_profiles
            WHERE id = ?
            """,
            (profile_id,),
        )
        if row is None:
            return None
        payload = dict(row)
        payload["parameters"] = json.loads(payload["parameters"])
        payload["donation_mode"] = bool(payload["donation_mode"])
        return payload

    async def delete_payment_profile(self, guild_id: int, name: str) -> Optional[int]:
        row = await self.fetch_one(
            "SELECT id FROM payment_profiles WHERE guild_id = ? AND name = ? COLLATE NOCASE",
            (guild_id, name),
        )
        if row is None:
            return None
        profile_id = int(row["id"])
        await self.execute("DELETE FROM payment_profiles WHERE id = ?", (profile_id,))
        return profile_id

    async def create_payment_session(
        self,
        guild_id: int,
        user_id: int,
        payment_profile_id: int,
        anonpay_id: str,
        status: str,
        status_url: str,
        checkout_url: str,
        webhook_url: Optional[str],
        expires_at: datetime,
        payload: dict[str, Any],
    ) -> int:
        now = utc_now().isoformat()
        await self.execute(
            """
            INSERT INTO payment_sessions (
                guild_id, user_id, payment_profile_id, anonpay_id, status, status_url, checkout_url, webhook_url,
                expires_at, created_at, last_status_check, last_status, last_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                payment_profile_id,
                anonpay_id,
                status,
                status_url,
                checkout_url,
                webhook_url,
                expires_at.isoformat(),
                now,
                now,
                status,
                json.dumps(payload),
            ),
        )
        row = await self.fetch_one(
            """
            SELECT id FROM payment_sessions
            WHERE guild_id = ? AND user_id = ? AND anonpay_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id, anonpay_id),
        )
        assert row is not None
        return int(row["id"])

    async def update_payment_session_status(
        self,
        session_id: int,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        await self.execute(
            """
            UPDATE payment_sessions
            SET status = ?, last_status = ?, last_status_check = ?, last_payload = ?
            WHERE id = ?
            """,
            (
                status,
                status,
                utc_now().isoformat(),
                json.dumps(payload),
                session_id,
            ),
        )

    async def update_payment_session_status_check(self, session_id: int) -> None:
        await self.execute(
            """
            UPDATE payment_sessions
            SET last_status_check = ?
            WHERE id = ?
            """,
            (
                utc_now().isoformat(),
                session_id,
            ),
        )

    async def list_active_sessions(self, cutoff: datetime) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            """
            SELECT id, guild_id, user_id, payment_profile_id, anonpay_id, status, status_url, checkout_url,
                   webhook_url, expires_at, created_at, last_status_check, last_status, last_payload
            FROM payment_sessions
            WHERE expires_at >= ?
            """,
            (cutoff.isoformat(),),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["expires_at"] = datetime.fromisoformat(payload["expires_at"])
            if payload["last_status_check"]:
                payload["last_status_check"] = datetime.fromisoformat(payload["last_status_check"])
            payload["last_payload"] = json.loads(payload["last_payload"]) if payload["last_payload"] else None
            results.append(payload)
        return results

    async def delete_payment_session(self, session_id: int) -> None:
        await self.execute("DELETE FROM payment_sessions WHERE id = ?", (session_id,))

    async def upsert_subscription(
        self,
        guild_id: int,
        user_id: int,
        payment_profile_id: int,
        role_id: Optional[int],
        expires_at: datetime,
        webhook_url: Optional[str],
    ) -> None:
        now = utc_now().isoformat()
        await self.execute(
            """
            INSERT INTO subscriptions (guild_id, user_id, payment_profile_id, role_id, expires_at, created_at, webhook_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, payment_profile_id) DO UPDATE SET
                role_id = excluded.role_id,
                expires_at = excluded.expires_at,
                webhook_url = excluded.webhook_url,
                last_notified_at = NULL
            """,
            (
                guild_id,
                user_id,
                payment_profile_id,
                role_id,
                expires_at.isoformat(),
                now,
                webhook_url,
            ),
        )

    async def list_expiring_subscriptions(self, reference_time: datetime, advance_notice: int) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            """
            SELECT id, guild_id, user_id, payment_profile_id, role_id, expires_at, last_notified_at, webhook_url
            FROM subscriptions
            WHERE expires_at BETWEEN ? AND ?
              AND (last_notified_at IS NULL OR last_notified_at < ?)
            """,
            (
                reference_time.isoformat(),
                (reference_time + timedelta(days=advance_notice)).isoformat(),
                (reference_time - timedelta(minutes=1)).isoformat(),
            ),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["expires_at"] = datetime.fromisoformat(payload["expires_at"])
            if payload["last_notified_at"]:
                payload["last_notified_at"] = datetime.fromisoformat(payload["last_notified_at"])
            results.append(payload)
        return results

    async def mark_subscription_notified(self, subscription_id: int) -> None:
        await self.execute(
            """
            UPDATE subscriptions
            SET last_notified_at = ?
            WHERE id = ?
            """,
            (
                utc_now().isoformat(),
                subscription_id,
            ),
        )

    async def list_expired_subscriptions(self, reference_time: datetime) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            """
            SELECT id, guild_id, user_id, payment_profile_id, role_id, expires_at, webhook_url
            FROM subscriptions
            WHERE expires_at <= ?
            """,
            (reference_time.isoformat(),),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["expires_at"] = datetime.fromisoformat(payload["expires_at"])
            results.append(payload)
        return results

    async def delete_subscription(self, subscription_id: int) -> None:
        await self.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))

    async def list_subscriptions_for_profile(self, profile_id: int) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            """
            SELECT id, guild_id, user_id, role_id, expires_at, webhook_url
            FROM subscriptions
            WHERE payment_profile_id = ?
            """,
            (profile_id,),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["expires_at"] = datetime.fromisoformat(payload["expires_at"])
            results.append(payload)
        return results
