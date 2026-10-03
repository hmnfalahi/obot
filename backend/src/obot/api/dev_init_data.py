"""Print signed initData for opening the mini app in a browser during development.

    uv run python -m obot.api.dev_init_data 272476753 > ../miniapp/.env.local

The output is a credential for that user (valid 24 hours): keep it out of git.
"""

import sys

from obot.api.auth import sign_init_data
from obot.config import get_settings


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m obot.api.dev_init_data <telegram user id>")
    token = get_settings().factory_bot_token
    if token is None:
        raise SystemExit("OBOT_FACTORY_BOT_TOKEN is not set")
    user = {"id": int(sys.argv[1]), "first_name": "Dev", "language_code": "fa"}
    print(f"VITE_DEV_INIT_DATA={sign_init_data(user, token.get_secret_value())}")


if __name__ == "__main__":
    main()
