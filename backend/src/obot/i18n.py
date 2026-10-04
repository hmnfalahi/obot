"""Minimal message catalog (fa/en) until the mini app brings full i18n."""

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "quota.daily_messages": (
            "You've used today's free messages. Top up credits to keep going, or come back "
            "tomorrow."
        ),
        "quota.bot_daily_cap": "You've reached the daily limit you set for this bot.",
        "quota.no_credits": "You're out of credits. Top up to keep going.",
        "btn.top_up": "Top up",
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
            "Commands: /mybots — your bots, /buy — buy credits, /cancel — drop the current draft"
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
        "card.hint": "Send a message to change anything.",
        "btn.create": "✅ Create bot",
        "btn.save": "💾 Save",
        "btn.cancel": "✖️ Cancel",
        "btn.panel": "⚙️ Edit in panel",
        "btn.create_keyboard": "Create {name}",
        "btn.create_link": "Create {name} (link)",
        "btn.open": "Open @{username}",
        "tool.web_search": "web search",
        "tool.read_url": "read web pages",
        "tool.rss": "RSS feeds",
        "tool.memory": "memory",
        "forget.done": "Done — I forgot everything about this chat.",
        "group.start_first": "To use me, start me in private first.",
        "task.draft": "⏰ New task: {summary}\nNext run: {when}\nSends to: {where}",
        "task.to_me": "you, here",
        "avatar.offer": "Want a profile photo for @{username}?",
        "btn.avatar_upload": "📷 Upload a photo",
        "btn.later": "Later",
        "avatar.choose_bot": "Which bot gets a new photo?",
        "avatar.send_photo": (
            "Send the photo for @{username}. I'll crop it to a square. (Any time later you can "
            "also send a photo captioned @{username}.)"
        ),
        "avatar.invalid": "That isn't an image I can use. Please send a photo.",
        "avatar.done": "✅ @{username} has its new photo.",
        "avatar.failed": "Telegram did not accept the photo for @{username}. Try another one.",
        "apikey.needed": "To do this I need your own {names}. Set it once and I'll remember it:",
        "btn.key_in_chat": "✍️ Send {name} here",
        "btn.key_in_panel": "⚙️ Set it in the panel",
        "apikey.send_now": (
            "Send me your {name} as your next message. I'll store it encrypted and delete "
            "your message right away. Send any /command to cancel."
        ),
        "apikey.saved": "✅ Your {name} is saved. Ask me again.",
        "bot.settings": "Your own key, model and daily limit for me are in your Obot panel.",
        "btn.my_settings": "Open my settings",
        "factory.my_settings": "Your settings for that bot:",
        "bot.hello": (
            "Hi! I'm {name}. {description}\n\nAsk me anything. I can also do things for you on "
            "a schedule, e.g. every morning; /tasks lists them."
        ),
        "btn.task_confirm": "✅ Confirm",
        "btn.task_cancel": "❌ Cancel",
        "task.confirmed": "✅ Scheduled: {summary} (next run: {when})",
        "task.cancelled": "Cancelled.",
        "task.cannot_confirm": "⚠️ Could not start it: {reason}",
        "task.not_yours": "This task is not yours.",
        "tasks.list_header": "Your tasks with this bot:",
        "tasks.list_empty": "You have no tasks with this bot. Just tell me what to do and when.",
        "tasks.paused": "paused",
        "tasks.next_run": "next: {when}",
        "task.failed": '⚠️ Your task "{summary}" did not run: {error}',
        "bot.private": "This bot is private; only its owner and people they chose can use it.",
        "access.pick": (
            "Choose up to {limit} people who may use @{username}. "
            "Your choice replaces the current list."
        ),
        "btn.pick_people": "Choose people",
        "access.saved": "Done — @{username} can now be used by: {names}.",
        "access.not_synced": (
            "Saved, but Telegram did not accept the change yet. Try again from the panel later."
        ),
        "access.unknown_bot": "That bot is not yours.",
        "btn.start_bot": "Start",
        "report.usage": "Usage: /report <what is wrong>",
        "report.factory_usage": "Usage: /report @bot_username <what is wrong>",
        "report.thanks": "Thanks, the report was sent to the Obot team.",
        "report.limit": "You have sent many reports today. Please try again tomorrow.",
        "report.not_found": "There is no Obot bot with that username.",
        "owner.bot_disabled": (
            "⛔ @{username} was disabled by the Obot team for breaking the rules."
        ),
        "owner.bot_enabled": "✅ @{username} was enabled again.",
        "owner.banned": "⛔ Your Obot account was suspended for breaking the rules.",
        "owner.task_failed": '⚠️ Task "{summary}" of @{username} failed: {error}',
        "forget.admins_only": "Only group admins can do that.",
        "buy.menu": (
            "Your credit balance: {balance}\n\n"
            "Buying credits moves you to the paid plan: every reply, search and task then "
            "uses credits, stronger models unlock and the limits are higher."
        ),
        "buy.button": "{stars} ⭐ → {credits} credits",
        "buy.title": "{credits} Obot credits",
        "buy.description": "{credits} credits for your Obot bots: replies, web search and tasks.",
        "buy.done": "✅ {credits} credits added. Balance: {balance}.",
        "buy.manual": (
            "Can't buy Stars? Pay by card instead:\n\n{text}\n\n"
            "Then send a photo of the receipt here and we'll add your credits."
        ),
        "receipt.received": "Receipt received. We'll add your credits after checking it.",
        "receipt.granted": "✅ {credits} credits added. Balance: {balance}.",
        "receipt.rejected": "Your receipt could not be confirmed. Please contact support.",
        "byok.chat_failed": (
            "I couldn't reply with your own key: {reason}. Check it in your Obot panel."
        ),
        "model.suggest": (
            "Heads-up: this bot works best with a stronger model than your plan includes. "
            "Top up credits, or set your own key for it in your Obot panel."
        ),
        "byok.reason.auth": "the key is invalid or was revoked",
        "byok.reason.quota": "the account is out of credit or rate-limited",
        "byok.reason.model": "the model was not found or the key cannot use it",
        "byok.reason.provider": "the provider returned an error or did not respond",
    },
    "fa": {
        "quota.daily_messages": (
            "پیام‌های رایگان امروزت تموم شده. برای ادامه اعتبار بخر یا فردا برگرد."
        ),
        "quota.bot_daily_cap": "به سقف مصرف روزانه‌ای که برای این ربات گذاشتی رسیدی.",
        "quota.no_credits": "اعتبارت تموم شده. برای ادامه شارژ کن.",
        "btn.top_up": "شارژ",
        "factory.bot_limit": (
            "الان {limit} ربات فعال داری که سقف پلنته. این ربات ساخته شد ولی غیرفعاله."
        ),
        "factory.at_limit": "الان {limit} ربات فعال داری که سقف پلنته.",
        "factory.welcome": (
            "سلام! من برات ربات هوش مصنوعی می‌سازم.\n\n"
            "فقط بگو رباتت چی کار کنه، مثلاً:\n"
            "• هر روز ساعت ۸ صبح قیمت دلار رو از tgju.org بگیر و برام بفرست\n"
            "• یه معلم زبان انگلیسی مهربون که اشتباهاتم رو درست کنه\n\n"
            "دستورها: /mybots — ربات‌هات، /buy — خرید اعتبار، /cancel — حذف پیش‌نویس فعلی"
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
        "card.hint": "برای تغییر هر چیزی پیام بده.",
        "btn.create": "✅ ساخت ربات",
        "btn.save": "💾 ذخیره",
        "btn.cancel": "✖️ لغو",
        "btn.panel": "⚙️ ویرایش در پنل",
        "btn.create_keyboard": "ساخت {name}",
        "btn.create_link": "ساخت {name} (لینک)",
        "btn.open": "باز کردن @{username}",
        "tool.web_search": "جستجوی وب",
        "tool.read_url": "خواندن صفحه‌ی وب",
        "tool.rss": "خبرخوان RSS",
        "tool.memory": "حافظه",
        "forget.done": "انجام شد — هر چی درباره‌ی این چت می‌دونستم پاک شد.",
        "group.start_first": "برای استفاده از من، اول توی پیوی استارتم کن.",
        "task.draft": "⏰ کار جدید: {summary}\nاجرای بعدی: {when}\nارسال به: {where}",
        "task.to_me": "خودت، همین‌جا",
        "avatar.offer": "برای @{username} عکس پروفایل می‌خوای؟",
        "btn.avatar_upload": "📷 آپلود عکس",
        "btn.later": "بعداً",
        "avatar.choose_bot": "عکس جدید برای کدوم ربات؟",
        "avatar.send_photo": (
            "عکس @{username} رو بفرست؛ مربعش می‌کنم. (بعداً هم هر وقت خواستی می‌تونی عکسی با "
            "کپشن @{username} بفرستی.)"
        ),
        "avatar.invalid": "این عکسی نیست که بتونم استفاده کنم. لطفاً یه عکس بفرست.",
        "avatar.done": "✅ عکس جدید @{username} گذاشته شد.",
        "avatar.failed": "تلگرام عکس @{username} رو قبول نکرد. یه عکس دیگه امتحان کن.",
        "apikey.needed": "برای این کار به {names} خودت نیاز دارم. یک بار تنظیمش کن، یادم می‌مونه:",
        "btn.key_in_chat": "✍️ فرستادن {name} همین‌جا",
        "btn.key_in_panel": "⚙️ تنظیم در پنل",
        "apikey.send_now": (
            "{name} خودت رو توی پیام بعدی بفرست. رمزنگاری‌شده نگهش می‌دارم و پیامت رو فوراً "
            "پاک می‌کنم. برای لغو هر دستوری با / بفرست."
        ),
        "apikey.saved": "✅ {name} تو ذخیره شد. دوباره ازم بخواه.",
        "bot.settings": "کلید و مدل خودت و سقف مصرف روزانه‌ات برای من توی پنل Obot هست.",
        "btn.my_settings": "باز کردن تنظیماتم",
        "factory.my_settings": "تنظیمات تو برای اون ربات:",
        "bot.hello": (
            "سلام! من {name} هستم. {description}\n\nهر چی می‌خوای بپرس. می‌تونم کارهایی رو هم "
            "سر وقت برات انجام بدم، مثلاً هر روز صبح؛ /tasks فهرستشون رو نشون می‌ده."
        ),
        "btn.task_confirm": "✅ تأیید",
        "btn.task_cancel": "❌ لغو",
        "task.confirmed": "✅ زمان‌بندی شد: {summary} (اجرای بعدی: {when})",
        "task.cancelled": "لغو شد.",
        "task.cannot_confirm": "⚠️ نشد شروعش کنم: {reason}",
        "task.not_yours": "این کار مال تو نیست.",
        "tasks.list_header": "کارهای تو با این ربات:",
        "tasks.list_empty": "با این ربات کاری نداری. کافیه بگی چی کار کنم و کی.",
        "tasks.paused": "متوقف",
        "tasks.next_run": "اجرای بعدی: {when}",
        "task.failed": "⚠️ کار «{summary}» اجرا نشد: {error}",
        "bot.private": "این ربات شخصیه؛ فقط صاحبش و کسایی که انتخاب کرده می‌تونن ازش استفاده کنن.",
        "access.pick": (
            "حداکثر {limit} نفر رو انتخاب کن که بتونن از @{username} استفاده کنن. "
            "انتخابت جای فهرست فعلی رو می‌گیره."
        ),
        "btn.pick_people": "انتخاب افراد",
        "access.saved": "انجام شد — این افراد می‌تونن از @{username} استفاده کنن: {names}.",
        "access.not_synced": (
            "ذخیره شد ولی تلگرام هنوز تغییر رو قبول نکرد. بعداً از پنل دوباره امتحان کن."
        ),
        "access.unknown_bot": "این ربات مال تو نیست.",
        "btn.start_bot": "استارت",
        "report.usage": "استفاده: /report <مشکل چیه>",
        "report.factory_usage": "استفاده: /report @bot_username <مشکل چیه>",
        "report.thanks": "ممنون، گزارش برای تیم Obot فرستاده شد.",
        "report.limit": "امروز گزارش زیادی فرستادی. لطفاً فردا دوباره امتحان کن.",
        "report.not_found": "رباتی با این یوزرنیم در Obot نیست.",
        "owner.bot_disabled": "⛔ @{username} به دلیل نقض قوانین توسط تیم Obot غیرفعال شد.",
        "owner.bot_enabled": "✅ @{username} دوباره فعال شد.",
        "owner.banned": "⛔ حساب Obot تو به دلیل نقض قوانین مسدود شد.",
        "owner.task_failed": "⚠️ کار «{summary}» از @{username} اجرا نشد: {error}",
        "forget.admins_only": "فقط ادمین‌های گروه می‌تونن این کار رو بکنن.",
        "buy.menu": (
            "موجودی اعتبارت: {balance}\n\n"
            "با خرید اعتبار به پلن پولی می‌ری: از اون به بعد هر پاسخ، جستجو و کار از اعتبار "
            "کم می‌شه، مدل‌های قوی‌تر باز می‌شن و سقف‌ها بالاتر می‌رن."
        ),
        "buy.button": "{stars} ⭐ ← {credits} اعتبار",
        "buy.title": "{credits} اعتبار Obot",
        "buy.description": "{credits} اعتبار برای ربات‌های Obot: پاسخ‌ها، جستجوی وب و کارها.",
        "buy.done": "✅ {credits} اعتبار اضافه شد. موجودی: {balance}.",
        "buy.manual": (
            "نمی‌تونی Stars بخری؟ کارت‌به‌کارت کن:\n\n{text}\n\n"
            "بعد عکس رسید رو همین‌جا بفرست تا اعتبارت اضافه بشه."
        ),
        "receipt.received": "رسید دریافت شد. بعد از بررسی اعتبارت اضافه می‌شه.",
        "receipt.granted": "✅ {credits} اعتبار اضافه شد. موجودی: {balance}.",
        "receipt.rejected": "رسیدت تأیید نشد. لطفاً با پشتیبانی تماس بگیر.",
        "byok.chat_failed": "نتونستم با کلید خودت جواب بدم: {reason}. توی پنل Obot بررسیش کن.",
        "model.suggest": (
            "این ربات با مدلی قوی‌تر از پلن تو بهتر کار می‌کنه. اعتبار بخر یا توی پنل Obot "
            "کلید خودت رو براش بذار."
        ),
        "byok.reason.auth": "کلید نامعتبره یا باطل شده",
        "byok.reason.quota": "اعتبار حساب تموم شده یا محدودیت نرخ خورده",
        "byok.reason.model": "مدل پیدا نشد یا این کلید به اون دسترسی نداره",
        "byok.reason.provider": "سرویس‌دهنده خطا داد یا جواب نداد",
    },
}


def lang_of(language_code: str | None) -> str:
    return "fa" if (language_code or "").startswith("fa") else "en"


def t(lang: str, key: str, **kwargs: object) -> str:
    template = MESSAGES.get(lang, MESSAGES["en"]).get(key) or MESSAGES["en"][key]
    return template.format(**kwargs)
