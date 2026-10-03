"""Minimal message catalog (fa/en) until the mini app brings full i18n."""

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "quota.daily_messages": "I've reached today's message limit. I'll be back tomorrow!",
        "quota.bot_daily_cap": "I've reached today's usage limit. I'll be back tomorrow!",
        "quota.no_credits": "I'm out of credits right now. Please try again later.",
        "owner.quota.daily_messages": (
            "Your bots used all {limit} free messages for today. They will answer again tomorrow."
        ),
        "owner.quota.bot_daily_cap": "@{username} reached its daily usage cap.",
        "owner.quota.no_credits": "Your credit balance is empty, so your bots stopped replying.",
        "factory.bot_limit": (
            "You already have {limit} active bots, the limit of your plan. "
            "This bot was created but is suspended."
        ),
        "factory.at_limit": "You already have {limit} active bots, the limit of your plan.",
    },
    "fa": {
        "quota.daily_messages": "سهمیه‌ی پیام امروزم تموم شده. فردا برمی‌گردم!",
        "quota.bot_daily_cap": "سقف مصرف امروزم پر شده. فردا برمی‌گردم!",
        "quota.no_credits": "الان اعتبارم تموم شده. لطفاً بعداً دوباره امتحان کن.",
        "owner.quota.daily_messages": (
            "ربات‌هات همه‌ی {limit} پیام رایگان امروز رو مصرف کردن. از فردا دوباره جواب می‌دن."
        ),
        "owner.quota.bot_daily_cap": "@{username} به سقف مصرف روزانه‌اش رسید.",
        "owner.quota.no_credits": "اعتبارت تموم شده و ربات‌هات دیگه جواب نمی‌دن.",
        "factory.bot_limit": (
            "الان {limit} ربات فعال داری که سقف پلنته. این ربات ساخته شد ولی غیرفعاله."
        ),
        "factory.at_limit": "الان {limit} ربات فعال داری که سقف پلنته.",
    },
}


def lang_of(language_code: str | None) -> str:
    return "fa" if (language_code or "").startswith("fa") else "en"


def t(lang: str, key: str, **kwargs: object) -> str:
    template = MESSAGES.get(lang, MESSAGES["en"]).get(key) or MESSAGES["en"][key]
    return template.format(**kwargs)
