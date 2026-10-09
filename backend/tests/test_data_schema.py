from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from obot.data.errors import OutsideAllowedHours, RecordInvalid, SchemaChangeRejected
from obot.data.schema import (
    CollectionSchema,
    check_allowed_hours,
    check_evolution,
    unique_keys,
    validate_record,
)

TEHRAN = ZoneInfo("Asia/Tehran")


def appointments() -> CollectionSchema:
    return CollectionSchema.model_validate(
        {
            "fields": [
                {"key": "name", "label": {"en": "Name"}, "type": "text", "required": True},
                {"key": "phone", "label": {"en": "Phone"}, "type": "phone"},
                {
                    "key": "date",
                    "label": {"en": "Date"},
                    "type": "date",
                    "required": True,
                    "public": True,
                },
                {
                    "key": "time",
                    "label": {"en": "Time"},
                    "type": "time",
                    "required": True,
                    "public": True,
                },
                {
                    "key": "status",
                    "label": {"en": "Status"},
                    "type": "choice",
                    "options": ["booked", "done"],
                },
            ],
            "rules": {
                "unique": [["date", "time"]],
                "allowed_hours": {
                    "date_field": "date",
                    "time_field": "time",
                    # Saturday-Wednesday 16-20, Thursday 9-12
                    "windows": [
                        {"weekdays": [5, 6, 0, 1, 2], "start": "16:00", "end": "20:00"},
                        {"weekdays": [3], "start": "09:00", "end": "12:00"},
                    ],
                },
            },
        }
    )


def expenses() -> CollectionSchema:
    return CollectionSchema.model_validate(
        {
            "fields": [
                {
                    "key": "amount",
                    "label": {"en": "Amount"},
                    "type": "money",
                    "required": True,
                    "min": 0,
                    "currency": "IRT",
                },
                {
                    "key": "category",
                    "label": {"en": "Category"},
                    "type": "choice",
                    "options": ["food", "rent"],
                },
                {"key": "at", "label": {"en": "When"}, "type": "datetime"},
                {"key": "paid", "label": {"en": "Paid"}, "type": "boolean"},
            ]
        }
    )


def test_normalizes_persian_digits_and_types() -> None:
    record = validate_record(
        expenses(),
        {"amount": "۲۵۰٬۰۰۰", "category": "food", "at": "2026-10-09T13:30", "paid": "بله"},
        TEHRAN,
    )
    assert record == {
        "amount": "250000",
        "category": "food",
        # 13:30 in Tehran (UTC+3:30) is 10:00 UTC
        "at": "2026-10-09T10:00:00Z",
        "paid": True,
    }


def test_reports_every_problem() -> None:
    with pytest.raises(RecordInvalid) as caught:
        validate_record(expenses(), {"amount": "-5", "category": "cars", "color": "red"}, TEHRAN)
    fields = sorted(e.field for e in caught.value.errors)
    assert fields == ["amount", "category", "color"]


def test_required_fields_on_create_and_partial_update() -> None:
    with pytest.raises(RecordInvalid) as caught:
        validate_record(appointments(), {"name": "Ali"}, TEHRAN)
    assert sorted(e.field for e in caught.value.errors) == ["date", "time"]

    current = validate_record(
        appointments(), {"name": "Ali", "date": "2026-10-10", "time": "17:00"}, TEHRAN
    )
    updated = validate_record(appointments(), {"time": "18:00"}, TEHRAN, current=current)
    assert updated == {"name": "Ali", "date": "2026-10-10", "time": "18:00"}
    with pytest.raises(RecordInvalid):
        validate_record(appointments(), {"name": None}, TEHRAN, current=current)


def test_hidden_fields_are_not_writable() -> None:
    schema = expenses()
    schema.fields[1].hidden = True
    with pytest.raises(RecordInvalid):
        validate_record(schema, {"amount": 1, "category": "food"}, TEHRAN)


def test_allowed_hours() -> None:
    schema = appointments()
    # 2026-10-10 is a Saturday
    check_allowed_hours(schema, {"date": "2026-10-10", "time": "17:00"}, TEHRAN)
    with pytest.raises(OutsideAllowedHours):
        check_allowed_hours(schema, {"date": "2026-10-10", "time": "20:00"}, TEHRAN)
    # 2026-10-16 is a Friday
    with pytest.raises(OutsideAllowedHours):
        check_allowed_hours(schema, {"date": "2026-10-16", "time": "17:00"}, TEHRAN)


def test_unique_keys_skip_missing_values() -> None:
    schema = appointments()
    keys = unique_keys(schema, {"date": "2026-10-10", "time": "17:00"})
    assert [rule for rule, _ in keys] == ["date,time"]
    assert keys == unique_keys(schema, {"date": "2026-10-10", "time": "17:00", "name": "X"})
    assert unique_keys(schema, {"date": "2026-10-10"}) == []


def test_invalid_schemas_are_refused() -> None:
    with pytest.raises(ValidationError):
        CollectionSchema.model_validate(
            {"fields": [{"key": "c", "label": {"en": "C"}, "type": "choice"}]}
        )
    with pytest.raises(ValidationError):
        CollectionSchema.model_validate(
            {
                "fields": [{"key": "a", "label": {"en": "A"}, "type": "text"}],
                "rules": {"unique": [["missing"]]},
            }
        )
    with pytest.raises(ValidationError):
        CollectionSchema.model_validate(
            {"fields": [{"key": "Bad Key", "label": {"en": "A"}, "type": "text"}]}
        )


def test_evolution_is_additive_once_records_exist() -> None:
    old = expenses()

    def changed(edit) -> CollectionSchema:
        d = old.model_dump(mode="json")
        edit(d)
        return CollectionSchema.model_validate(d)

    # Without records anything goes.
    replacement = CollectionSchema.model_validate(
        {"fields": [{"key": "x", "label": {"en": "X"}, "type": "text", "required": True}]}
    )
    check_evolution(old, replacement, has_records=False)

    def grow(d) -> None:
        d["fields"].append({"key": "note", "label": {"en": "Note"}, "type": "text"})
        d["fields"][1]["options"].append("travel")
        d["fields"][1]["hidden"] = True
        d["fields"][0]["min"] = None  # widened

    check_evolution(old, changed(grow), has_records=True)

    for edit in (
        lambda d: d["fields"].pop(1),
        lambda d: d["fields"][0].update(type="number"),
        lambda d: d["fields"][2].update(required=True),
        lambda d: d["fields"][1].update(options=["food"]),
        lambda d: d["fields"][0].update(min="10"),
        lambda d: d["fields"].append(
            {"key": "n", "label": {"en": "N"}, "type": "text", "required": True}
        ),
        lambda d: d["rules"].update(unique=[["category"]]),
    ):
        with pytest.raises(SchemaChangeRejected):
            check_evolution(old, changed(edit), has_records=True)
