"""Date and number formatting in Habr style.

Dates for saved files (:func:`format_datetime`, :func:`format_date`) are always in Russian, as on
the site; short dates and durations for the UI use the current UI language.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from .i18n import text, tr

_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def format_datetime(value: datetime | None, tz: tzinfo | None = None) -> str:
    """Format as ``16 окт 2026 в 12:30`` in time zone ``tz`` (local by default)."""
    if value is None:
        return ""
    local = value.astimezone(tz) if value.tzinfo else value
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year} в {local:%H:%M}"


def format_date(value: datetime | None, tz: tzinfo | None = None) -> str:
    """Format a date without time: ``16 окт 2026``."""
    if value is None:
        return ""
    local = value.astimezone(tz) if value.tzinfo else value
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def format_short_datetime(
    value: datetime | None, tz: tzinfo | None = None, now: datetime | None = None
) -> str:
    """Format briefly for cards.

    ``16 окт, 12:30`` in the current year, ``16 окт 2025`` in earlier ones.
    """
    if value is None:
        return ""
    local = value.astimezone(tz) if value.tzinfo else value
    now = now or datetime.now(local.tzinfo)
    month = text("months")[local.month - 1]
    if local.year != now.year:
        return tr("date.short.other_year", day=local.day, month=month, year=local.year)
    return tr("date.short.current_year", day=local.day, month=month, time=f"{local:%H:%M}")


def format_count(value: int | None) -> str:
    """Abbreviate large numbers: ``1290 -> 1.3K``, ``25905 -> 26K``, ``1500000 -> 1.5M``."""
    if value is None:
        return "0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1000:
        return f"{sign}{value}"
    for divider, suffix in ((1_000_000, "M"), (1000, "K")):
        if value >= divider:
            scaled = value / divider
            text = f"{scaled:.1f}" if scaled < 10 else f"{scaled:.0f}"
            return f"{sign}{text.removesuffix('.0')}{suffix}"
    return f"{sign}{value}"  # pragma: no cover - unreachable


def format_score(value: int | float | None) -> str:
    """Format a vote score with a sign: ``+43``, ``-2``, ``0``."""
    if not value:
        return "0"
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return f"+{text}" if value > 0 else text


def format_number(value: int | float | None) -> str:
    """Format karma or rating: integers without a fractional part, fractions to two digits."""
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(int(value))


def format_duration(seconds: float) -> str:
    """Format how long the work took: ``40 s``, ``7 min 40 s``, ``1 h 5 min``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return tr("duration.h_m", h=hours, m=minutes) if minutes else tr("duration.h", h=hours)
    if minutes:
        return tr("duration.m_s", m=minutes, s=secs) if secs else tr("duration.m", m=minutes)
    return tr("duration.s", s=secs)


def format_eta(seconds: float) -> str:
    """Format the time remaining: ``less than a minute``, ``about 6 min``, ``about 1 h 20 min``."""
    if seconds < 60:
        return tr("eta.less_minute")
    minutes = -(-int(seconds) // 60)  # round up: "about 1 min" is better than promising 0
    hours, minutes = divmod(minutes, 60)
    return tr("eta.about", duration=format_duration((hours * 60 + minutes) * 60))


def plural(n: int, one: str, few: str, many: str) -> str:
    """Return the Russian plural form: 1 комментарий, 2 комментария, 5 комментариев."""
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many
