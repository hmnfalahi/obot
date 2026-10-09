"""Errors of the data layer. Each has a stable `code` the bot tools and the API turn into words."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obot.data.schema import AllowedHours


class DataError(Exception):
    code = "data_error"


class CollectionNotFound(DataError):
    code = "collection_not_found"

    def __init__(self, key: str) -> None:
        super().__init__(f"no collection named {key!r}")
        self.key = key


class RecordNotFound(DataError):
    code = "record_not_found"

    def __init__(self, record_id: int) -> None:
        super().__init__(f"no record {record_id}")
        self.record_id = record_id


class NotAllowed(DataError):
    code = "not_allowed"


class InvalidSchema(DataError):
    code = "invalid_schema"


class SchemaChangeRejected(DataError):
    """`change` says what was refused, for wording it to people: removed | type_change |
    unhide_required | currency_change | became_required | options_removed | limits_narrowed |
    required_added | unique_added. `field` is the field's key (None for unique_added). The
    message is for the model and logs."""

    code = "schema_change_rejected"

    def __init__(self, message: str, *, change: str, field: str | None = None) -> None:
        super().__init__(message)
        self.change = change
        self.field = field


class InvalidQuery(DataError):
    code = "invalid_query"


class StaleRecord(DataError):
    code = "stale"

    def __init__(self) -> None:
        super().__init__("the record changed since it was read")


class LimitReached(DataError):
    code = "limit_reached"

    def __init__(self, limit: str) -> None:
        super().__init__(f"plan limit reached: {limit}")
        self.limit = limit  # "collections" | "records" | "record_bytes"


class DuplicateRecord(DataError):
    code = "duplicate"

    def __init__(self, fields: list[str]) -> None:
        super().__init__(f"a record with the same {', '.join(fields)} exists")
        self.fields = fields


class OutsideAllowedHours(DataError):
    code = "outside_allowed_hours"

    def __init__(self, allowed: "AllowedHours") -> None:
        super().__init__("outside the allowed hours")
        self.allowed = allowed


@dataclass(frozen=True)
class FieldError:
    field: str
    reason: str


class RecordInvalid(DataError):
    code = "invalid"

    def __init__(self, errors: list[FieldError]) -> None:
        super().__init__("; ".join(f"{e.field}: {e.reason}" for e in errors))
        self.errors = errors
