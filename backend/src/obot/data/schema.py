"""Collection schemas: fields, rules, and turning loose input into stored values (SPEC 8.8).

Pure functions, no database. Stored formats: numbers and money as decimal strings, dates
YYYY-MM-DD, times HH:MM, datetimes in UTC as YYYY-MM-DDTHH:MM:SSZ, booleans as JSON bools.
ISO strings of one type compare correctly as text, which obot.data.query relies on.
"""

import enum
import hashlib
import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from obot.data.errors import (
    FieldError,
    OutsideAllowedHours,
    RecordInvalid,
    SchemaChangeRejected,
)

KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
MAX_FIELDS = 30
MAX_TEXT = 2000

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TRUE = {"true", "yes", "y", "1", "بله", "آره", "اره", "هست"}
_FALSE = {"false", "no", "n", "0", "خیر", "نه", "نیست"}


class FieldType(enum.StrEnum):
    TEXT = "text"
    NUMBER = "number"
    MONEY = "money"
    DATE = "date"
    TIME = "time"
    DATETIME = "datetime"
    CHOICE = "choice"
    BOOLEAN = "boolean"
    PHONE = "phone"


NUMERIC_TYPES = frozenset({FieldType.NUMBER, FieldType.MONEY})
# Types whose input may use Persian or Arabic-Indic digits. Text and choice keep their characters.
_DIGIT_TYPES = frozenset(
    {
        FieldType.NUMBER,
        FieldType.MONEY,
        FieldType.DATE,
        FieldType.TIME,
        FieldType.DATETIME,
        FieldType.PHONE,
        FieldType.BOOLEAN,
    }
)
# Persian decimal separator becomes ".", Persian/Arabic/ASCII thousands separators are dropped.
_NUMBER_SEPARATORS = str.maketrans({"٫": ".", "٬": None, "،": None, ",": None})
MAX_INT_DIGITS = 18
MAX_FRACTION_DIGITS = 10


class FieldDef(BaseModel):
    key: str = Field(pattern=KEY_PATTERN)
    label: dict[str, str]  # by language, e.g. {"fa": "مبلغ", "en": "Amount"}
    type: FieldType
    required: bool = False
    options: list[str] = []  # choice fields only
    min: Decimal | None = None  # number and money only
    max: Decimal | None = None
    currency: str | None = None  # money only, e.g. "IRT", "USD"
    # Shown to everyone in availability answers of a shared collection (e.g. date and time).
    public: bool = False
    # Kept in stored records but no longer shown or written; fields are never removed.
    hidden: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "FieldDef":
        if (self.type == FieldType.CHOICE) != bool(self.options):
            raise ValueError(f"{self.key}: choice fields, and only they, have options")
        if (self.min is not None or self.max is not None) and self.type not in NUMERIC_TYPES:
            raise ValueError(f"{self.key}: only number and money fields have min/max")
        return self


class Window(BaseModel):
    weekdays: list[int] = Field(min_length=1)  # 0 = Monday ... 6 = Sunday
    start: time
    end: time  # exclusive

    @model_validator(mode="after")
    def _consistent(self) -> "Window":
        if any(d not in range(7) for d in self.weekdays):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        if self.start >= self.end:
            raise ValueError("a window must start before it ends")
        return self


class AllowedHours(BaseModel):
    # A date field plus a time field, or a single datetime field (time_field None).
    date_field: str
    time_field: str | None = None
    windows: list[Window] = Field(min_length=1)


class Rules(BaseModel):
    unique: list[list[str]] = []
    allowed_hours: AllowedHours | None = None

    @field_validator("unique")
    @classmethod
    def _non_empty(cls, groups: list[list[str]]) -> list[list[str]]:
        if any(not g for g in groups):
            raise ValueError("a unique rule needs at least one field")
        return groups


class CollectionSchema(BaseModel):
    fields: list[FieldDef] = Field(min_length=1, max_length=MAX_FIELDS)
    rules: Rules = Rules()

    @model_validator(mode="after")
    def _consistent(self) -> "CollectionSchema":
        by_key = self.field_map()
        if len(by_key) != len(self.fields):
            raise ValueError("field keys must be unique")
        if any(k not in by_key for group in self.rules.unique for k in group):
            raise ValueError("a unique rule names an unknown field")
        if (allowed := self.rules.allowed_hours) is not None:
            day = by_key.get(allowed.date_field)
            if allowed.time_field is None:
                ok = day is not None and day.type == FieldType.DATETIME
            else:
                at = by_key.get(allowed.time_field)
                ok = (
                    day is not None
                    and day.type == FieldType.DATE
                    and at is not None
                    and at.type == FieldType.TIME
                )
            if not ok:
                raise ValueError("allowed_hours needs a datetime field, or a date and a time field")
        return self

    def field_map(self) -> dict[str, FieldDef]:
        return {f.key: f for f in self.fields}

    def field(self, key: str) -> FieldDef | None:
        return self.field_map().get(key)


def _plain_decimal(number: Decimal) -> str:
    """A plain decimal string with no exponent: at most 18 integer and 10 fractional digits.

    Size is checked from the digits and exponent before any formatting, so a huge exponent
    is never expanded.
    """
    if number == 0:
        return "0"
    if number.adjusted() >= MAX_INT_DIGITS:
        raise ValueError("number too large or too precise")
    _, digits, exponent = number.as_tuple()
    digits = tuple(digits)
    while digits and digits[-1] == 0:
        digits, exponent = digits[:-1], exponent + 1
    if -exponent > MAX_FRACTION_DIGITS:
        raise ValueError("number too large or too precise")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def normalize_value(f: FieldDef, value: Any, tz: ZoneInfo, *, check_limits: bool = True) -> Any:
    """The stored form of `value` for `f`. ValueError with a short reason if it does not fit.

    `check_limits` False skips min/max, for values used in filters rather than stored.
    """
    if isinstance(value, str):
        value = value.strip()
        if f.type in _DIGIT_TYPES:
            value = value.translate(_DIGITS)
    match f.type:
        case FieldType.TEXT:
            text = str(value)
            if len(text) > MAX_TEXT:
                raise ValueError(f"longer than {MAX_TEXT} characters")
            return text
        case FieldType.NUMBER | FieldType.MONEY:
            if isinstance(value, bool):
                raise ValueError("not a number")
            try:
                number = Decimal(str(value).translate(_NUMBER_SEPARATORS))
            except (InvalidOperation, ArithmeticError):
                raise ValueError("not a number") from None
            if not number.is_finite():
                raise ValueError("not a number")
            text = _plain_decimal(number)
            if check_limits and f.min is not None and number < f.min:
                raise ValueError(f"must be at least {f.min}")
            if check_limits and f.max is not None and number > f.max:
                raise ValueError(f"must be at most {f.max}")
            return text
        case FieldType.DATE:
            try:
                return date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ValueError("not a date (YYYY-MM-DD)") from None
        case FieldType.TIME:
            try:
                return time.fromisoformat(str(value)).strftime("%H:%M")
            except ValueError:
                raise ValueError("not a time (HH:MM)") from None
        case FieldType.DATETIME:
            try:
                moment = (
                    value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
                )
            except ValueError:
                raise ValueError("not a date and time (YYYY-MM-DDTHH:MM)") from None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=tz)
            return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        case FieldType.CHOICE:
            choice = str(value)
            if choice not in f.options:
                raise ValueError(f"must be one of: {', '.join(f.options)}")
            return choice
        case FieldType.BOOLEAN:
            if isinstance(value, bool):
                return value
            word = str(value).lower()
            if word in _TRUE:
                return True
            if word in _FALSE:
                return False
            raise ValueError("not yes or no")
        case FieldType.PHONE:
            phone = re.sub(r"[\s\-()]", "", str(value))
            if not re.fullmatch(r"\+?[0-9]{7,15}", phone):
                raise ValueError("not a phone number")
            return phone


def validate_record(
    schema: CollectionSchema,
    data: dict[str, Any],
    tz: ZoneInfo,
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge `data` into `current` (None for a new record) and return the record to store.

    None or "" clears an optional field. Raises RecordInvalid listing every problem.
    """
    fields = schema.field_map()
    errors: list[FieldError] = []
    result = dict(current or {})
    for key, value in data.items():
        f = fields.get(key)
        if f is None or f.hidden:
            errors.append(FieldError(key, "unknown field"))
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            result.pop(key, None)
            continue
        try:
            result[key] = normalize_value(f, value, tz)
        except (ValueError, TypeError) as e:
            errors.append(FieldError(key, str(e) or "invalid value"))
    # A field that failed to normalize already has its error.
    failed = {e.field for e in errors}
    for f in schema.fields:
        if f.required and not f.hidden and f.key not in result and f.key not in failed:
            errors.append(FieldError(f.key, "required"))
    if errors:
        raise RecordInvalid(errors)
    return result


def check_allowed_hours(schema: CollectionSchema, record: dict[str, Any], tz: ZoneInfo) -> None:
    """Raise OutsideAllowedHours unless the record's day and time fall in a window.

    A date + time pair is local wall-clock time; a datetime field is converted to `tz`.
    """
    allowed = schema.rules.allowed_hours
    if allowed is None or allowed.date_field not in record:
        return
    if allowed.time_field is None:
        local = datetime.fromisoformat(record[allowed.date_field]).astimezone(tz)
        weekday, at = local.weekday(), local.time()
    else:
        if allowed.time_field not in record:
            return
        weekday = date.fromisoformat(record[allowed.date_field]).weekday()
        at = time.fromisoformat(record[allowed.time_field])
    if not any(weekday in w.weekdays and w.start <= at < w.end for w in allowed.windows):
        raise OutsideAllowedHours(allowed)


def unique_keys(schema: CollectionSchema, record: dict[str, Any]) -> list[tuple[str, str]]:
    """(rule, value hash) for each unique rule; rules with a missing value are skipped,
    like NULLs in a SQL unique index."""
    keys = []
    for group in schema.rules.unique:
        values = [record.get(k) for k in group]
        if any(v is None for v in values):
            continue
        digest = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
        keys.append((",".join(group), digest))
    return keys


def check_evolution(old: CollectionSchema, new: CollectionSchema, *, has_records: bool) -> None:
    """Raise SchemaChangeRejected unless every record valid under `old` stays valid under `new`."""
    if not has_records:
        return
    new_fields = new.field_map()
    for f in old.fields:
        n = new_fields.get(f.key)
        if n is None:
            raise SchemaChangeRejected(f"{f.key}: fields cannot be removed; hide it instead")
        if n.type != f.type:
            raise SchemaChangeRejected(f"{f.key}: the type of a field cannot change")
        if n.required and not f.required:
            raise SchemaChangeRejected(f"{f.key}: an existing field cannot become required")
        if set(f.options) - set(n.options):
            raise SchemaChangeRejected(f"{f.key}: choice options can only be added")
        if n.min is not None and (f.min is None or n.min > f.min):
            raise SchemaChangeRejected(f"{f.key}: limits can only be widened")
        if n.max is not None and (f.max is None or n.max < f.max):
            raise SchemaChangeRejected(f"{f.key}: limits can only be widened")
    old_keys = old.field_map()
    for n in new.fields:
        if n.key not in old_keys and n.required:
            raise SchemaChangeRejected(f"{n.key}: new fields must be optional")
    old_unique = {tuple(g) for g in old.rules.unique}
    if any(tuple(g) not in old_unique for g in new.rules.unique):
        raise SchemaChangeRejected("unique rules cannot be added once a collection has records")
