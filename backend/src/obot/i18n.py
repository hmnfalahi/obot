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
        "factory.welcome": (
            "Hi! I build AI bots for you.\n\n"
            "Just tell me what your bot should do, for example:\n"
            "• Every morning at 8, get the dollar price from tgju.org and send it to me\n"
            "• A friendly English tutor that corrects my mistakes\n\n"
            "Commands: /mybots — your bots, /cancel — drop the current draft"
        ),
        "factory.mybots_empty": "You have no bots yet. Describe one and I'll build it.",
        "factory.mybots_header": 'Your bots (send a message like "make @bot more formal" to edit):',
        "factory.cancelled": "Draft dropped.",
        "factory.nothing_to_cancel": "There is no draft to drop.",
        "factory.daily_limit": "You've sent a lot of messages today. Please continue tomorrow.",
        "factory.error": "Sorry, something went wrong. Please try again.",
        "factory.stale": "This draft is no longer active.",
        "factory.press_create": "Tap the button below to create the bot in Telegram.",
        "factory.link_fallback": "Button not working (e.g. on iOS)? Use this link:",
        "factory.ready": "✅ @{username} is ready.",
        "factory.open_hint": "Open it and press Start so it can message you.",
        "factory.saved": "✅ Changes to @{username} saved.",
        "card.new": "📝 New bot draft",
        "card.edit": "📝 Changes to @{username}",
        "card.tools": "Tools",
        "card.tasks": "Tasks",
        "card.tasks_note": "",
        "card.hint": "Send a message to change anything.",
        "btn.create": "✅ Create bot",
        "btn.save": "💾 Save",
        "btn.cancel": "✖️ Cancel",
        "btn.create_keyboard": "Create {name}",
        "btn.create_link": "Create {name} (link)",
        "btn.open": "Open @{username}",
        "tool.web_search": "web search",
        "tool.read_url": "read web pages",
        "tool.rss": "RSS feeds",
        "tool.memory": "memory",
        "forget.done": "Done — I forgot everything about this chat.",
        "tasks.scheduled": "⏰ Scheduled for @{username}:",
        "tasks.next": "next: {when}",
        "tasks.rejected": "⚠️ Not scheduled:",
        "tasks.waiting": "Tasks that message you start after you press Start in @{username}.",
        "tasks.none": "@{username} has no scheduled tasks now.",
        "owner.task_waiting": (
            "@{username} has a scheduled task for you, but you haven't pressed Start in it yet."
        ),
        "owner.task_failed": '⚠️ Task "{summary}" of @{username} failed: {error}',
        "forget.admins_only": "Only group admins can do that.",
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
        "factory.welcome": (
            "سلام! من برات ربات هوش مصنوعی می‌سازم.\n\n"
            "فقط بگو رباتت چی کار کنه، مثلاً:\n"
            "• هر روز ساعت ۸ صبح قیمت دلار رو از tgju.org بگیر و برام بفرست\n"
            "• یه معلم زبان انگلیسی مهربون که اشتباهاتم رو درست کنه\n\n"
            "دستورها: /mybots — ربات‌هات، /cancel — حذف پیش‌نویس فعلی"
        ),
        "factory.mybots_empty": "هنوز رباتی نداری. توضیح بده چی می‌خوای تا بسازمش.",
        "factory.mybots_header": "ربات‌هات (برای ویرایش مثلاً بنویس «@bot رو رسمی‌تر کن»):",
        "factory.cancelled": "پیش‌نویس حذف شد.",
        "factory.nothing_to_cancel": "پیش‌نویسی برای حذف نیست.",
        "factory.daily_limit": "امروز پیام زیادی فرستادی. لطفاً فردا ادامه بده.",
        "factory.error": "ببخشید، مشکلی پیش اومد. لطفاً دوباره امتحان کن.",
        "factory.stale": "این پیش‌نویس دیگه فعال نیست.",
        "factory.press_create": "دکمه‌ی زیر رو بزن تا ربات توی تلگرام ساخته بشه.",
        "factory.link_fallback": "دکمه کار نمی‌کنه (مثلاً روی iOS)؟ از این لینک استفاده کن:",
        "factory.ready": "✅ @{username} آماده‌ست.",
        "factory.open_hint": "بازش کن و Start رو بزن تا بتونه بهت پیام بده.",
        "factory.saved": "✅ تغییرات @{username} ذخیره شد.",
        "card.new": "📝 پیش‌نویس ربات جدید",
        "card.edit": "📝 تغییرات @{username}",
        "card.tools": "ابزارها",
        "card.tasks": "کارها",
        "card.tasks_note": "",
        "card.hint": "برای تغییر هر چیزی پیام بده.",
        "btn.create": "✅ ساخت ربات",
        "btn.save": "💾 ذخیره",
        "btn.cancel": "✖️ لغو",
        "btn.create_keyboard": "ساخت {name}",
        "btn.create_link": "ساخت {name} (لینک)",
        "btn.open": "باز کردن @{username}",
        "tool.web_search": "جستجوی وب",
        "tool.read_url": "خواندن صفحه‌ی وب",
        "tool.rss": "خبرخوان RSS",
        "tool.memory": "حافظه",
        "forget.done": "انجام شد — هر چی درباره‌ی این چت می‌دونستم پاک شد.",
        "tasks.scheduled": "⏰ زمان‌بندی‌شده برای @{username}:",
        "tasks.next": "اجرای بعدی: {when}",
        "tasks.rejected": "⚠️ زمان‌بندی نشد:",
        "tasks.waiting": (
            "کارهایی که برای تو پیام می‌فرستن، بعد از زدن Start در @{username} شروع می‌شن."
        ),
        "tasks.none": "@{username} الان کار زمان‌بندی‌شده‌ای نداره.",
        "owner.task_waiting": (
            "@{username} یه کار زمان‌بندی‌شده برات داره، ولی هنوز Start رو توش نزدی."
        ),
        "owner.task_failed": "⚠️ کار «{summary}» از @{username} اجرا نشد: {error}",
        "forget.admins_only": "فقط ادمین‌های گروه می‌تونن این کار رو بکنن.",
    },
}


def lang_of(language_code: str | None) -> str:
    return "fa" if (language_code or "").startswith("fa") else "en"


def t(lang: str, key: str, **kwargs: object) -> str:
    template = MESSAGES.get(lang, MESSAGES["en"]).get(key) or MESSAGES["en"][key]
    return template.format(**kwargs)
