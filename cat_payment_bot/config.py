from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class Settings:
    """Runtime configuration values loaded from the environment."""

    discord_token: str
    database_path: str = "cat_payment_bot.db"
    status_poll_interval: int = 60
    subscription_check_interval: int = 3600
    session_ttl_minutes: int = 20
    request_timeout: int = 30
    user_agent: str = "CatPaymentBot/1.0"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            raise RuntimeError(
                "DISCORD_TOKEN is not set. Please provide your Discord bot token."
            )

        database_path = os.getenv("DATABASE_PATH", cls.__dataclass_fields__["database_path"].default)  # type: ignore[index]
        status_poll_interval = int(os.getenv("STATUS_POLL_INTERVAL", cls.__dataclass_fields__["status_poll_interval"].default))  # type: ignore[index]
        subscription_check_interval = int(os.getenv("SUBSCRIPTION_CHECK_INTERVAL", cls.__dataclass_fields__["subscription_check_interval"].default))  # type: ignore[index]
        session_ttl_minutes = int(os.getenv("SESSION_TTL_MINUTES", cls.__dataclass_fields__["session_ttl_minutes"].default))  # type: ignore[index]
        request_timeout = int(os.getenv("REQUEST_TIMEOUT", cls.__dataclass_fields__["request_timeout"].default))  # type: ignore[index]
        user_agent = os.getenv("USER_AGENT", cls.__dataclass_fields__["user_agent"].default)  # type: ignore[index]

        return cls(
            discord_token=token,
            database_path=database_path,
            status_poll_interval=status_poll_interval,
            subscription_check_interval=subscription_check_interval,
            session_ttl_minutes=session_ttl_minutes,
            request_timeout=request_timeout,
            user_agent=user_agent,
        )

