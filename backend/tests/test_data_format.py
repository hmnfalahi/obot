from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from obot.data.errors import (
    CollectionNotFound,
    DataError,
    DuplicateRecord,
    FieldError,
    LimitReached,
    OutsideAllowedHours,
    RecordInvalid,
    RecordNotFound,
    StaleRecord,
)
from obot.data.format import (
    describe_collections,
    describe_error,
    format_records,
    format_summary,
    format_taken,
)
from obot.data.query import Metric, SummaryRow
from obot.data.schema import AllowedHours, CollectionSchema
from obot.data.space import DataSpace
from obot.db.models import Collection, CollectionScope, Record

TEHRAN = ZoneInfo("Asia/Tehran")

EXPENSES = CollectionSchema.model_validate(
    {
        "fields": [
            {
                "key": "amount",
                "label": {"fa": "مبلغ", "en": "Amount"},
                "type": "money",
                "required": True,
                "currency": "IRT",
                "min": 0,
            },
            {
                "key": "category",
                "label": {"en": "Category"},
                "type": "choice",
                "options": ["food", "rent"],
            },
            {"key": "old", "label": {"en": "Old"}, "type": "text", "hidden": True},
        ]
    }
)
BOOKINGS = CollectionSchema.model_validate(
    {
        "fields": [
            {"key": "name", "label": {"en": "Name"}, "type": "text", "required": True},
            {"key": "date", "label": {"en": "Date"}, "type": "date", "public": True},
            {"key": "time", "label": {"en": "Time"}, "type": "time", "public": True},
        ],
        "rules": {
            "unique": [["date", "time"]],
            "allowed_hours": {
                "date_field": "date",
                "time_field": "time",
                "windows": [{"weekdays": [5, 6, 0], "start": "16:00", "end": "20:00"}],
            },
        },
    }
)


def collection(key, scope, schema, title) -> Collection:
    dumped = schema.model_dump(mode="json")
    return Collection(
        id=1,
        bot_id=1,
        key=key,
        title=title,
        scope=scope,
        schema={"fields": dumped["fields"]},
        rules=dumped["rules"],
        schema_version=1,
    )


def space(
    user_id: int, timezone: str = "Asia/Tehran", owner_timezone: str = "Asia/Tehran"
) -> DataSpace:
    return DataSpace(
        bot_id=1,
        owner_id=1,
        user_id=user_id,
        timezone=timezone,
        owner_timezone=owner_timezone,
        plan="free",
        owner_plan="free",
    )


NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)  # Friday 11:30 in Tehran


def test_catalog_describes_fields_rules_and_scope() -> None:
    note = describe_collections(
        [
            collection("expenses", CollectionScope.PERSONAL, EXPENSES, {"en": "Expenses"}),
            collection("bookings", CollectionScope.SHARED, BOOKINGS, {"en": "Appointments"}),
        ],
        space(user_id=2),
        NOW,
    )
    assert 'expenses "Expenses"' in note
    assert "amount (money IRT, min 0, required)" in note
    assert "category (choice: food|rent)" in note
    assert "old (" not in note  # hidden fields are not shown
    assert "each user has their own records" in note
    assert "only this user's own records" in note and "availability=true" in note
    assert "at most one record per date+time" in note
    assert "Sat,Sun,Mon 16:00-20:00" in note
    assert "public fields: date, time" in note
    assert "Friday 2026-10-09" in note and "Asia/Tehran" in note
    assert "Sat,Sun,Mon 16:00-20:00 (end excluded)" in note
    assert note.count("(end excluded)") == 1
    assert note.count("Times are in Asia/Tehran.") == 2
    assert "In Asia/Tehran it is" not in note  # same zone for user and owner
    assert "Allowed times (" not in note


def test_each_collection_states_its_own_time_zone() -> None:
    note = describe_collections(
        [
            collection("expenses", CollectionScope.PERSONAL, EXPENSES, {"en": "Expenses"}),
            collection("bookings", CollectionScope.SHARED, BOOKINGS, {"en": "Appointments"}),
        ],
        space(user_id=2, timezone="Europe/Berlin", owner_timezone="Asia/Tehran"),
        NOW,
    )
    lines = note.splitlines()
    expenses = next(line for line in lines if line.startswith("- expenses"))
    bookings = next(line for line in lines if line.startswith("- bookings"))
    assert "Times are in Europe/Berlin." in expenses
    assert "Times are in Asia/Tehran." in bookings
    # 08:00 UTC: 10:00 in Berlin, 11:30 in Tehran, both on Friday 2026-10-09
    assert lines[-2].endswith("Today is Friday 2026-10-09, 10:00 in Europe/Berlin.")
    assert lines[-1] == "In Asia/Tehran it is Friday 2026-10-09, 11:30."
    assert "local time of the collection" in note
    assert "in the user's local time" not in note


def test_catalog_for_the_owner_says_they_see_everything() -> None:
    note = describe_collections(
        [collection("bookings", CollectionScope.SHARED, BOOKINGS, {"en": "Appointments"})],
        space(user_id=1),
        NOW,
    )
    assert "owns this bot and sees everyone's records" in note


def test_hidden_fields_are_left_out_of_records() -> None:
    record = Record(
        id=3,
        data={"amount": "5", "old": "secret"},
        created_at=datetime(2026, 10, 9, 8, 0, tzinfo=UTC),
    )
    assert format_records([record], TEHRAN, hidden=["old"]) == ('#3 (2026-10-09) {"amount": "5"}')
    assert format_records([record], TEHRAN, ("old", "amount")) == "#3 (2026-10-09) {}"


def test_records_and_taken_rows() -> None:
    record = Record(
        id=12,
        data={"amount": "250000", "category": "food"},
        created_at=datetime(2026, 10, 8, 22, 0, tzinfo=UTC),
    )
    assert format_records([record], TEHRAN) == (
        '#12 (2026-10-09) {"amount": "250000", "category": "food"}'
    )
    assert format_records([], TEHRAN) == "No records."
    assert format_taken([{"date": "2026-10-10", "time": "17:00"}]) == (
        'Taken: {"date": "2026-10-10", "time": "17:00"}'
    )
    assert format_taken([]) == "Nothing taken matches."


def test_summaries() -> None:
    assert format_summary([SummaryRow(None, Decimal("1120.00"))], Metric.SUM) == "sum: 1120"
    assert (
        format_summary(
            [SummaryRow("food", Decimal("2")), SummaryRow(None, Decimal("1"))], Metric.COUNT
        )
        == "count by group:\nfood: 2\n(empty): 1"
    )
    assert format_summary([SummaryRow(None, Decimal("2.333333"))], Metric.AVG) == "avg: 2.33"


def test_errors_read_well() -> None:
    allowed = AllowedHours.model_validate(BOOKINGS.rules.allowed_hours.model_dump())
    cases = {
        RecordInvalid([FieldError("amount", "not a number")]): (
            "invalid values: amount: not a number"
        ),
        DuplicateRecord(["date", "time"]): "the same date+time already exists",
        OutsideAllowedHours(allowed): "allowed times: Sat,Sun,Mon 16:00-20:00",
        LimitReached("records"): "record limit",
        CollectionNotFound("x"): "no collection named 'x'",
        RecordNotFound(5): "no record #5",
        StaleRecord(): "the record changed meanwhile; look it up again with data_find",
        DataError("boom."): "ERROR: boom.",
    }
    for error, expected in cases.items():
        text = describe_error(error)
        assert text.startswith("ERROR: ") and expected in text, text
    assert describe_error(DataError("boom.")) == "ERROR: boom."
