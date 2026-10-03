from urllib.parse import quote


def create_link(factory_username: str, suggested_username: str, name: str) -> str:
    """t.me link that opens Telegram's managed-bot creation dialog (works on iOS too)."""
    return f"https://t.me/newbot/{factory_username}/{suggested_username}?name={quote(name)}"
