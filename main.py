from __future__ import annotations

from cat_payment_bot.bot import create_bot
from cat_payment_bot.config import Settings

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def main() -> None:
    if load_dotenv:
        load_dotenv()
    settings = Settings.from_env()
    bot = create_bot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
