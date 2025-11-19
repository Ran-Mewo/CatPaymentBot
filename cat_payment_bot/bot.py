from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands
from discord.ext import commands

from .anonpay import AnonpayClient, AnonpayError
from .config import Settings
from .database import Database, utc_now
from .services import FINAL_STATUSES, PaymentManager, PaymentSession

log = logging.getLogger("catpaymentbot")


class CatPaymentBot(commands.Bot):
    SUCCESS_COLOR = discord.Color(0x2ECC71)
    ERROR_COLOR = discord.Color(0xE74C3C)
    WARNING_COLOR = discord.Color(0xF1C40F)
    INFO_COLOR = discord.Color(0x5865F2)
    ACCENT_COLOR = discord.Color(0x1ABC9C)

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = False

        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)

        self.settings = settings
        self.db = Database(settings.database_path)
        self.anonpay = AnonpayClient(timeout=settings.request_timeout, user_agent=settings.user_agent)
        self.manager = PaymentManager(settings, self.db, self.anonpay)
        self._status_task: Optional[asyncio.Task] = None
        self._subscription_task: Optional[asyncio.Task] = None

        self.tree.on_error = self._on_app_command_error

        self._register_commands()

    def _build_embed(
        self,
        description: str,
        *,
        title: Optional[str] = None,
        color: Optional[discord.Color] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color or self.INFO_COLOR,
            timestamp=utc_now(),
        )
        return embed

    def _register_commands(self) -> None:
        @self.tree.command(name="setup", description="Configure the payout address, coin, and network for this server.")
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.guild_only()
        async def setup(
            interaction: discord.Interaction,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not interaction.guild:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        "This command can only be used inside a server.",
                        title="Guild Only",
                        color=self.ERROR_COLOR,
                    )
                )
                return

            existing = await self.manager.ensure_guild_setup(interaction.guild.id)
            instructions = (
                "Follow the steps below to configure this server's payout settings:\n"
                "1. Open the [AnonPay URL Generator](https://trocador.app/en/anonpayurlgenerator).\n"
                "2. Fill in the required parameters (coin and payout address), then press **Update**.\n"
                "3. Copy the **Regular** payment URL and submit it using the button below."
            )
            embed = self._build_embed(
                instructions,
                title="Server Setup Required",
                color=self.INFO_COLOR,
            )
            if existing:
                embed.add_field(
                    name="Current Settings",
                    value=(
                        f"Address: `{existing['payout_address']}`\n"
                        f"Coin: `{existing['ticker_to']}`\n"
                        f"Network: `{existing['network_to']}`"
                    ),
                    inline=False,
                )
            embed.set_footer(text="Only manage server members can submit configuration changes.")

            view = self._SetupParametersView(
                bot=self,
                guild_id=interaction.guild.id,
                requester_id=interaction.user.id,
            )

            await interaction.edit_original_response(embed=embed, view=view)

        @self.tree.command(name="create", description="Create a payment or donation template.")
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.guild_only()
        @app_commands.describe(
            name="Unique identifier for this payment template.",
            role="Role granted after successful payment.",
            duration_days="Subscription length in days (optional).",
            amount="Amount of the coin to charge or preset donate amount.",
            memo="Memo/ExtraID value if required by the network (use 0 to omit).",
            donation="Enable donation mode instead of locked payment.",
            ticker_from="Preselected coin ticker for the payer.",
            network_from="Preselected network for the payer.",
            description="Description displayed on the checkout page (URL-encoded).",
            ref="Affiliate referral code.",
            buttonbgcolor="Hex color (without #) for the checkout button background.",
            textcolor="Hex color (without #) for the button text.",
            bgcolor="Background color or set to true for gray.",
            email="Email to receive payment confirmations.",
            fiat_equiv="Fiat currency abbreviation for price display (USD, EUR, etc.).",
            remove_direct_pay="Disable direct payments in the recipient coin.",
            min_logpolicy="Restrict to providers with minimum log policy rating (A, B, or C).",
            webhook="Webhook URL to mirror status updates.",
            simple_mode="Enable streamlined checkout screen.",
            maximum="Maximum USD amount allowed for donations.",
        )
        async def create(
            interaction: discord.Interaction,
            name: str,
            role: Optional[discord.Role] = None,
            duration_days: Optional[app_commands.Range[int, 1, 3650]] = None,
            amount: Optional[float] = None,
            memo: Optional[str] = None,
            donation: Optional[bool] = None,
            ticker_from: Optional[str] = None,
            network_from: Optional[str] = None,
            description: Optional[str] = None,
            ref: Optional[str] = None,
            buttonbgcolor: Optional[str] = None,
            textcolor: Optional[str] = None,
            bgcolor: Optional[str] = None,
            email: Optional[str] = None,
            fiat_equiv: Optional[str] = None,
            remove_direct_pay: Optional[bool] = None,
            min_logpolicy: Optional[str] = None,
            webhook: Optional[str] = None,
            simple_mode: Optional[bool] = None,
            maximum: Optional[float] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            guild = interaction.guild
            if guild is None:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        "This command can only be used inside a server.",
                        title="Guild Only",
                        color=self.ERROR_COLOR,
                    )
                )
                return

            setup_info = await self.manager.ensure_guild_setup(guild.id)
            if not setup_info:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        "Please run `/setup` first to configure the payout address, coin, and network.",
                        title="Setup Required",
                        color=self.WARNING_COLOR,
                    )
                )
                return

            params: dict[str, Any] = {}
            optional_fields = {
                "amount": amount,
                "memo": memo,
                "donation": donation,
                "ticker_from": ticker_from,
                "network_from": network_from,
                "description": description,
                "ref": ref,
                "buttonbgcolor": buttonbgcolor,
                "textcolor": textcolor,
                "bgcolor": bgcolor,
                "email": email,
                "fiat_equiv": fiat_equiv,
                "remove_direct_pay": remove_direct_pay,
                "min_logpolicy": min_logpolicy,
                "webhook": webhook,
                "simple_mode": simple_mode,
                "maximum": maximum,
            }
            for key, value in optional_fields.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    params[key] = value
                elif isinstance(value, float):
                    params[key] = f"{value:.12g}"
                else:
                    if key in {"ticker_from", "network_from"}:
                        params[key] = str(value).upper()
                    else:
                        params[key] = value

            try:
                await self.manager.create_payment_profile(
                    guild_id=guild.id,
                    name=name,
                    role=role,
                    duration_days=int(duration_days) if duration_days else None,
                    parameters=params,
                )
            except Exception as exc:  # pylint: disable=broad-except
                log.exception("Failed to create payment profile: guild=%s name=%s", guild.id, name)
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        f"Failed to create payment template: {exc}",
                        title="Creation Failed",
                        color=self.ERROR_COLOR,
                    )
                )
                return

            description_lines = [
                f"Created payment `{name}`.",
                f"Role assignment: {'none' if role is None else role.mention}.",
            ]
            if duration_days:
                description_lines.append(f"Subscription duration: {int(duration_days)} day(s).")
            if donation:
                description_lines.append("Mode: donation.")
            await interaction.edit_original_response(
                embed=self._build_embed(
                    "\n".join(description_lines),
                    title="Payment Created",
                    color=self.SUCCESS_COLOR,
                )
            )

        @self.tree.command(name="delete", description="Remove an existing payment template.")
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.guild_only()
        @app_commands.describe(name="Name of the payment template to remove.")
        @app_commands.autocomplete(name=self._payment_name_autocomplete)
        async def delete(
            interaction: discord.Interaction,
            name: str,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            guild = interaction.guild
            if guild is None:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        "This command can only be used inside a server.",
                        title="Guild Only",
                        color=self.ERROR_COLOR,
                    )
                )
                return

            profile = await self.manager.get_payment_profile(guild.id, name)
            if not profile:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        f"No payment named `{name}` was found.",
                        title="Payment Not Found",
                        color=self.WARNING_COLOR,
                    )
                )
                return

            subscriptions = await self.manager.list_subscriptions_for_profile(profile["id"])
            await self.manager.delete_payment_profile(guild.id, name)

            # Remove roles from active subscribers, if any
            if subscriptions:
                guild_obj = guild
                for subscription in subscriptions:
                    if not subscription["role_id"]:
                        continue
                    role = guild_obj.get_role(subscription["role_id"])
                    if not role:
                        continue
                    member = guild_obj.get_member(subscription["user_id"])
                    if not member:
                        continue
                    with contextlib.suppress(discord.HTTPException):
                        await member.remove_roles(role, reason="Payment template deleted")

            await interaction.edit_original_response(
                embed=self._build_embed(
                    f"Payment `{name}` and related data removed.",
                    title="Payment Deleted",
                    color=self.ACCENT_COLOR,
                )
            )

        @self.tree.command(name="pay", description="Initiate a payment using one of this server's templates.")
        @app_commands.guild_only()
        @app_commands.describe(name="Optional name of the payment template to use.")
        @app_commands.autocomplete(name=self._payment_name_autocomplete)
        async def pay(
            interaction: discord.Interaction,
            name: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            guild = interaction.guild
            if guild is None:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        "This command can only be used inside a server.",
                        title="Guild Only",
                        color=self.ERROR_COLOR,
                    )
                )
                return

            setup_info = await self.manager.ensure_guild_setup(guild.id)
            if not setup_info:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        "This server has not configured payments yet. Ask an admin to run `/setup`.",
                        title="Payments Disabled",
                        color=self.WARNING_COLOR,
                    )
                )
                return

            if name:
                profile = await self.manager.get_payment_profile(guild.id, name)
                if not profile:
                    await interaction.edit_original_response(
                        embed=self._build_embed(
                            f"No payment named `{name}` is available on this server.",
                            title="Payment Not Found",
                            color=self.WARNING_COLOR,
                        )
                    )
                    return
            else:
                profiles = await self.manager.list_payment_profiles(guild.id)
                if not profiles:
                    await interaction.edit_original_response(
                        embed=self._build_embed(
                            "This server has no payments configured.",
                            title="No Payments",
                            color=self.WARNING_COLOR,
                        )
                    )
                    return
                profile = profiles[0]
                name = profile["name"]

            try:
                session, _payload = await self.manager.start_payment_session(
                    guild_id=guild.id,
                    user_id=interaction.user.id,
                    profile=profile,
                    guild_settings=setup_info,
                )
            except AnonpayError as exc:
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        f"AnonPay rejected the request: {exc}",
                        title="Payment Error",
                        color=self.ERROR_COLOR,
                    )
                )
                return
            except Exception as exc:  # pylint: disable=broad-except
                log.exception("Unexpected error while creating payment session.")
                await interaction.edit_original_response(
                    embed=self._build_embed(
                        f"Could not start payment session: {exc}",
                        title="Payment Error",
                        color=self.ERROR_COLOR,
                    )
                )
                return

            checkout_url = session.checkout_url
            message = (
                f"Payment `{name}` initialized.\n"
                f"Use the button below to proceed (expires in {self.settings.session_ttl_minutes} minutes)."
            )
            embed = self._build_embed(
                message,
                title="Checkout Ready",
                color=self.INFO_COLOR,
            )
            embed.add_field(name="Checkout", value=f"[Open Payment Link]({checkout_url})", inline=False)
            await interaction.edit_original_response(embed=embed)

    async def _payment_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        profiles = await self.manager.list_payment_profiles(interaction.guild.id)
        current_lower = current.lower()
        options = []
        for profile in profiles:
            name = profile["name"]
            if current_lower and current_lower not in name.lower():
                continue
            options.append(app_commands.Choice(name=name, value=name))
            if len(options) >= 25:
                break
        return options

    def _parse_payment_url(self, payment_url: str) -> tuple[str, str, str]:
        normalized = payment_url.strip()
        if not normalized:
            raise ValueError("The payment URL cannot be empty.")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"https", "http"}:
            raise ValueError("The payment URL must start with http:// or https://.")
        params = parse_qs(parsed.query)

        def _extract_param(name: str) -> str:
            values = params.get(name)
            if not values or not values[0].strip():
                raise ValueError(f"The payment URL is missing the `{name}` parameter.")
            return values[0].strip()

        address = _extract_param("address")
        ticker_to = _extract_param("ticker_to").upper()
        network_to = _extract_param("network_to").upper()

        return address, ticker_to, network_to

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.tree.sync()
        self._status_task = asyncio.create_task(self._poll_payment_statuses(), name="payment-status-poll")
        self._subscription_task = asyncio.create_task(self._subscription_watchdog(), name="subscription-watchdog")

    async def close(self) -> None:
        tasks = [t for t in (self._status_task, self._subscription_task) if t]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self.anonpay.close()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def _poll_payment_statuses(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._process_payment_updates()
            except Exception as exc:  # pylint: disable=broad-except
                log.exception("Payment status polling failed: %s", exc)
            await asyncio.sleep(self.settings.status_poll_interval)

    async def _process_payment_updates(self) -> None:
        sessions = await self.manager.load_active_sessions()
        for session in sessions:
            previous_status = session.status

            if session.expires_at < utc_now() and previous_status not in FINAL_STATUSES:
                # Session expired locally; flag as expired without querying remote endpoint again.
                await self.manager.purge_session(session.id)
                await self._notify_user(
                    session,
                    "Payment session expired before completion.",
                    title="Payment Expired",
                    color=self.ERROR_COLOR,
                )
                continue

            try:
                payload = await self.manager.refresh_session_status(session)
            except AnonpayError as exc:
                log.warning("Failed to refresh status for session %s: %s", session.id, exc)
                continue

            status = session.status.lower()
            if status != previous_status:
                await self._handle_status_change(session, status, payload)

            if status in FINAL_STATUSES:
                await self.manager.purge_session(session.id)

    async def _handle_status_change(
        self,
        session: PaymentSession,
        status: str,
        payload: Optional[dict[str, Any]],
    ) -> None:
        payload = payload or {}

        if session.webhook_url:
            webhook_payload = dict(payload)
            webhook_payload["discord_id"] = str(session.user_id)
            try:
                await self.anonpay.post_webhook(session.webhook_url, webhook_payload)
            except AnonpayError as exc:
                log.warning("Webhook delivery failed for session %s: %s", session.id, exc)

        guild = self.get_guild(session.guild_id)
        profile = await self.manager.get_payment_profile_by_id(session.profile_id) if guild else None

        if status == "finished":
            await self._handle_successful_payment(session, guild, profile, payload)
        elif status == "paid partially":
            await self._notify_user(
                session,
                "Payment received partially. Contact support to resolve the difference.",
                title="Partial Payment",
                color=self.WARNING_COLOR,
            )
        elif status in {"failed", "expired", "halted", "refunded"}:
            await self._notify_user(
                session,
                f"Payment status updated: **{status}**.",
                title="Payment Update",
                color=self.ERROR_COLOR,
            )

    async def _handle_successful_payment(
        self,
        session: PaymentSession,
        guild: Optional[discord.Guild],
        profile: Optional[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        if not guild or not profile:
            return

        role_id = profile.get("role_id")
        role = guild.get_role(role_id) if role_id else None

        member = guild.get_member(session.user_id)
        if role and member:
            with contextlib.suppress(discord.HTTPException):
                await member.add_roles(role, reason="Payment completed")

        duration_days = profile.get("duration_days")
        webhook_url = session.webhook_url or profile["parameters"].get("webhook") if profile else None
        if duration_days:
            await self.manager.upsert_subscription(session, profile, webhook_url)

        if webhook_url:
            webhook_payload = dict(payload)
            webhook_payload["discord_id"] = str(session.user_id)
            webhook_payload["subscription_active"] = True
            try:
                await self.anonpay.post_webhook(webhook_url, webhook_payload)
            except AnonpayError as exc:
                log.warning("Webhook delivery failed for subscription activation: %s", exc)

    async def _notify_user(
        self,
        session: PaymentSession,
        message: str,
        *,
        title: Optional[str] = None,
        color: Optional[discord.Color] = None,
    ) -> None:
        user = self.get_user(session.user_id)
        if not user:
            try:
                user = await self.fetch_user(session.user_id)
            except discord.HTTPException:
                return
        with contextlib.suppress(discord.HTTPException):
            await user.send(embed=self._build_embed(message, title=title, color=color))

    async def _subscription_watchdog(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._process_subscriptions()
            except Exception as exc:  # pylint: disable=broad-except
                log.exception("Subscription processing failed: %s", exc)
            await asyncio.sleep(self.settings.subscription_check_interval)

    async def _process_subscriptions(self) -> None:
        now = utc_now()
        expiring = await self.manager.list_expiring_subscriptions(now, 1)
        for subscription in expiring:
            if subscription.get("last_notified_at"):
                continue
            await self._notify_subscription_expiring(subscription)
            await self.manager.mark_subscription_notified(subscription["id"])

        expired = await self.manager.list_expired_subscriptions(now)
        for subscription in expired:
            await self._expire_subscription(subscription)
            await self.manager.delete_subscription(subscription["id"])

    async def _notify_subscription_expiring(self, subscription: dict[str, Any]) -> None:
        guild = self.get_guild(subscription["guild_id"])
        if not guild:
            return
        profile = await self.manager.get_payment_profile_by_id(subscription["payment_profile_id"])
        payment_name = profile["name"] if profile else "unknown"
        guild_name = guild.name

        if subscription.get("webhook_url"):
            payload = {
                "event": "subscription_expiring",
                "discord_id": str(subscription["user_id"]),
                "guild_id": str(subscription["guild_id"]),
                "payment_name": payment_name,
                "guild_name": guild_name,
                "expires_at": subscription["expires_at"].isoformat(),
            }
            try:
                await self.anonpay.post_webhook(subscription["webhook_url"], payload)
            except AnonpayError as exc:
                log.warning("Webhook delivery failed for subscription expiring: %s", exc)

    async def _expire_subscription(self, subscription: dict[str, Any]) -> None:
        guild = self.get_guild(subscription["guild_id"])
        if not guild:
            return
        profile = await self.manager.get_payment_profile_by_id(subscription["payment_profile_id"])
        payment_name = profile["name"] if profile else "unknown"
        guild_name = guild.name
        role_id = subscription.get("role_id")
        role = guild.get_role(role_id) if role_id else None
        member = guild.get_member(subscription["user_id"])

        if member and role:
            with contextlib.suppress(discord.HTTPException):
                await member.remove_roles(role, reason="Subscription expired")

        webhook_url = subscription.get("webhook_url")
        if webhook_url:
            payload = {
                "event": "subscription_expired",
                "discord_id": str(subscription["user_id"]),
                "guild_id": str(subscription["guild_id"]),
                "payment_name": payment_name,
                "guild_name": guild_name,
                "expired_at": subscription["expires_at"].isoformat(),
            }
            try:
                await self.anonpay.post_webhook(webhook_url, payload)
            except AnonpayError as exc:
                log.warning("Webhook delivery failed for subscription expired: %s", exc)

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        log.exception("Unhandled error in %s", event_method, exc_info=True)

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "You do not have permission to use this command."
            title = "Permission Denied"
            color = self.ERROR_COLOR
        else:
            log.exception("App command error: %s", error)
            message = "Something went wrong while executing that command."
            title = "Command Error"
            color = self.ERROR_COLOR

        embed = self._build_embed(message, title=title, color=color)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    class _SetupParametersView(discord.ui.View):
        def __init__(self, bot: "CatPaymentBot", guild_id: int, requester_id: int) -> None:
            super().__init__(timeout=600)
            self.bot = bot
            self.guild_id = guild_id
            self.requester_id = requester_id

        @discord.ui.button(label="Submit Required Parameters", style=discord.ButtonStyle.primary)
        async def submit_parameters(  # type: ignore[override]
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message(
                    embed=self.bot._build_embed(
                        "Only the admin who ran `/setup` can submit these parameters.",
                        title="Not Allowed",
                        color=self.bot.WARNING_COLOR,
                    ),
                    ephemeral=True,
                )
                return

            await interaction.response.send_modal(
                self.bot._SetupParametersModal(
                    bot=self.bot,
                    guild_id=self.guild_id,
                    requester_id=self.requester_id,
                )
            )

    class _SetupParametersModal(discord.ui.Modal):
        def __init__(self, bot: "CatPaymentBot", guild_id: int, requester_id: int) -> None:
            super().__init__(title="Submit Required Parameters")
            self.bot = bot
            self.guild_id = guild_id
            self.requester_id = requester_id
            self.payment_url = discord.ui.TextInput(
                label="Regular Payment URL",
                placeholder="https://trocador.app/anonpay/?ticker_to=btc&network_to=Mainnet&address=...",
                style=discord.TextStyle.short,
                required=True,
                max_length=4000,
            )
            self.add_item(self.payment_url)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message(
                    embed=self.bot._build_embed(
                        "Only the admin who ran `/setup` can submit these parameters.",
                        title="Not Allowed",
                        color=self.bot.WARNING_COLOR,
                    ),
                    ephemeral=True,
                )
                return

            try:
                address, ticker_to, network_to = self.bot._parse_payment_url(self.payment_url.value)
            except ValueError as exc:
                await interaction.response.send_message(
                    embed=self.bot._build_embed(
                        str(exc),
                        title="Invalid Payment URL",
                        color=self.bot.ERROR_COLOR,
                    ),
                    ephemeral=True,
                )
                return

            await self.bot.manager.setup_guild(
                guild_id=self.guild_id,
                address=address,
                coin=ticker_to,
                network=network_to,
            )

            confirmation = self.bot._build_embed(
                "Server payment settings saved. New payments will use the configured address, coin, and network.",
                title="Setup Complete",
                color=self.bot.SUCCESS_COLOR,
            )
            confirmation.add_field(
                name="Configured Values",
                value=f"Address: `{address}`\nCoin: `{ticker_to}`\nNetwork: `{network_to}`",
                inline=False,
            )

            if interaction.message:
                await interaction.response.edit_message(embed=confirmation, view=None)
            else:
                await interaction.response.send_message(embed=confirmation, ephemeral=True)


def create_bot(settings: Settings) -> CatPaymentBot:
    logging.basicConfig(level=logging.INFO)
    return CatPaymentBot(settings)
