# CatPaymentBot

CatPaymentBot is a Discord bot that lets servers accept cryptocurrency payments and donations via [Trocador AnonPay](https://trocador.app/en/anonpay). Server admins can configure payout details, create payment templates, and automatically manage member roles and subscriptions when payments succeed.

## Features

- `/setup` – store the payout address, coin ticker, and network for a guild.
- `/create` – build reusable payment or donation methods with optional role assignment, subscription duration, and advanced AnonPay parameters.
- `/pay` – members start a payment and receive a checkout URL. The bot polls AnonPay for status changes, notifies the user, and assigns roles on success.
- `/delete` – remove payment methods, including active subscriptions and associated roles.
- Automated subscription monitoring notifies members 1 day before expiry and removes roles if they lapse. Optional webhooks receive mirrored updates for payments and subscriptions.

## Requirements

- Python 3.11+
- A Discord application with the bot scope and required permissions (manage roles if you plan to assign them).
- A Trocador-compatible wallet address for receiving funds.

## Setup

1. Create and populate a `.env` file based on `.env.example`.
2. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```
3. Activate the existing virtual environment (`.venv`) if you prefer:
   ```bash
   source .venv/bin/activate
   ```
4. Run the bot:
   ```bash
   python main.py
   ```

Invite the bot to your server, run `/setup`, then use `/create` to configure payment options for your community.

## Webhook Notifications

Payment templates that include the optional `webhook` field receive realtime JSON callbacks whenever the bot hears from Trocador or processes subscription events.

- **Payment status updates** – the bot forwards the AnonPay payload as-is and adds `discord_id` (payer) so you can correlate the transaction with a guild member. When a payment starts a subscription, an additional `subscription_active: true` flag is included.
- **Subscription expiring (1 day notice)** – a custom payload the bot emits with `event: "subscription_expiring"`, the member's `discord_id`, `guild_id`, the payment name, and the ISO `expires_at` timestamp.
- **Subscription expired** – triggered when access lapses. Payload includes `event: "subscription_expired"`, `discord_id`, `guild_id`, the payment name, and `expired_at`.

Every webhook request is an HTTP `POST` with a JSON body sent to the URL you supplied during `/create`, so you can centralise processing for both payment and lifecycle messages.
