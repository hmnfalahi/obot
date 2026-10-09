"""Collections in plain words for people: the factory's draft card and the chat cards.

The model reads obot.data.format instead; this module is for owners and users, in fa/en.
"""

from datetime import time

from obot.data.schema import AllowedHours, CollectionSchema, FieldDef
from obot.db.models import CollectionScope
from obot.i18n import t

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
# Persian weeks start on Saturday; days are listed in the week order of the language.
_WEEK_ORDER = {"fa": [5, 6, 0, 1, 2, 3, 4], "en": [0, 1, 2, 3, 4, 5, 6]}


def label_of(labels: dict[str, str], lang: str, fallback: str) -> str:
    return labels.get(lang) or next(iter(labels.values()), None) or fallback


def _separator(lang: str) -> str:
    return "، " if lang == "fa" else ", "


def _clock(value: time, lang: str) -> str:
    text = f"{value:%H:%M}"
    return text.translate(_FA_DIGITS) if lang == "fa" else text


def field_text(f: FieldDef, lang: str) -> str:
    kind = t(lang, f"ftype.{f.type.value}")
    if f.options:
        kind += ": " + _separator(lang).join(f.options)
    return f"{label_of(f.label, lang, f.key)} ({kind})"


def _windows_text(allowed: AllowedHours, lang: str) -> str:
    order = _WEEK_ORDER.get(lang, _WEEK_ORDER["en"])
    parts = []
    for w in allowed.windows:
        days = _separator(lang).join(
            t(lang, f"weekday.{d}") for d in sorted(w.weekdays, key=order.index)
        )
        parts.append(
            t(
                lang,
                "data.rule.window",
                days=days,
                start=_clock(w.start, lang),
                end=_clock(w.end, lang),
            )
        )
    return ("؛ " if lang == "fa" else "; ").join(parts)


def rules_text(schema: CollectionSchema, lang: str) -> list[str]:
    fields = schema.field_map()
    lines = [
        t(
            lang,
            "data.rule.unique",
            fields=" + ".join(label_of(fields[k].label, lang, k) for k in group),
        )
        for group in schema.rules.unique
    ]
    if schema.rules.allowed_hours is not None:
        lines.append(_windows_text(schema.rules.allowed_hours, lang))
    return lines


def collection_text(title: str, scope: CollectionScope, schema: CollectionSchema, lang: str) -> str:
    """A bullet with the title and who sees what, then its visible fields and rules."""
    lines = [f"• {title} — {t(lang, f'data.scope.{scope.value}')}"]
    visible = [field_text(f, lang) for f in schema.fields if not f.hidden]
    lines.append("   " + _separator(lang).join(visible))
    lines += [f"   {rule}" for rule in rules_text(schema, lang)]
    return "\n".join(lines)
