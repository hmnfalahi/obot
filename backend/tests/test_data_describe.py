from obot.data.describe import collection_text, field_text, label_of, rules_text
from obot.data.schema import CollectionSchema
from obot.db.models import CollectionScope

BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"fa": "نام", "en": "Name"}, "type": "text", "required": True},
            {"key": "date", "label": {"fa": "تاریخ", "en": "Date"}, "type": "date", "public": True},
            {"key": "time", "label": {"fa": "ساعت", "en": "Time"}, "type": "time", "public": True},
            {"key": "reason", "label": {"fa": "علت"}, "type": "choice", "options": ["چکاپ", "درد"]},
            {"key": "old", "label": {"en": "Old"}, "type": "text", "hidden": True},
        ],
        "rules": {
            "unique": [["date", "time"]],
            "allowed_hours": {
                "date_field": "date",
                "time_field": "time",
                "windows": [
                    {"weekdays": [5, 6, 0, 1, 2], "start": "16:00", "end": "20:00"},
                    {"weekdays": [3], "start": "09:00", "end": "12:00"},
                ],
            },
        },
    }
)


def test_labels_fall_back_to_any_language_then_the_key() -> None:
    assert label_of({"fa": "نام", "en": "Name"}, "en", "name") == "Name"
    assert label_of({"fa": "علت"}, "en", "reason") == "علت"
    assert label_of({}, "fa", "reason") == "reason"


def test_field_text() -> None:
    f = BOOKINGS.field_map()
    assert field_text(f["date"], "fa") == "تاریخ (تاریخ)"
    assert field_text(f["reason"], "fa") == "علت (انتخابی: چکاپ، درد)"
    assert field_text(f["reason"], "en") == "علت (choice: چکاپ, درد)"


def test_rules_in_plain_words() -> None:
    assert rules_text(BOOKINGS, "fa") == [
        "هر تاریخ + ساعت فقط یک بار",
        "شنبه، یکشنبه، دوشنبه، سه‌شنبه، چهارشنبه ۱۶:۰۰ تا ۲۰:۰۰؛ پنجشنبه ۰۹:۰۰ تا ۱۲:۰۰",
    ]
    assert rules_text(BOOKINGS, "en") == [
        "one per Date + Time",
        "Mon, Tue, Wed, Sat, Sun 16:00–20:00; Thu 09:00–12:00",
    ]


def test_collection_summary() -> None:
    text = collection_text("نوبت‌ها", CollectionScope.SHARED, BOOKINGS, "fa")
    assert text.splitlines()[0] == "• نوبت‌ها — همه ثبت می‌کنند، فقط شما همه را می‌بینید"
    assert "نام (متن)" in text and "Old" not in text
    assert "هر تاریخ + ساعت فقط یک بار" in text
    personal = collection_text("Expenses", CollectionScope.PERSONAL, BOOKINGS, "en")
    assert personal.splitlines()[0] == "• Expenses — each user keeps their own"
