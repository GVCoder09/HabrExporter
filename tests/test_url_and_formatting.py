from datetime import UTC, datetime, timedelta, timezone

import pytest

from habr_exporter.formatting import (
    format_count,
    format_date,
    format_datetime,
    format_duration,
    format_eta,
    format_number,
    format_score,
    format_short_datetime,
    plural,
)
from habr_exporter.url import (
    InvalidArticleUrlError,
    absolutize,
    canonical_article_url,
    parse_article_id,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://habr.com/ru/companies/k2tech/articles/1094766/", "1094766"),
        ("https://habr.com/ru/articles/123456/", "123456"),
        ("https://habr.com/ru/articles/123456", "123456"),
        ("habr.com/ru/news/777/", "777"),
        ("https://habr.com/en/post/42/", "42"),
        ("https://habr.com/p/31337/", "31337"),
        ("https://habr.com/ru/posts/555/?utm=1#comment_1", "555"),
        ("https://habrahabr.ru/company/x/blog/1234/", "1234"),
        ("https://m.habr.com/ru/articles/99/comments/", "99"),
        ("  1094766  ", "1094766"),
    ],
)
def test_parse_article_id(url, expected):
    assert parse_article_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://example.com/articles/1/",
        "https://habr.com/ru/hubs/python/",
        "habr.com/ru/users/x/",
    ],
)
def test_parse_article_id_rejects_garbage(url):
    with pytest.raises(InvalidArticleUrlError):
        parse_article_id(url)


def test_canonical_and_absolutize():
    assert canonical_article_url("1", "en") == "https://habr.com/en/articles/1/"
    assert absolutize("//habrastorage.org/a.png") == "https://habrastorage.org/a.png"
    assert absolutize("/ru/articles/1/") == "https://habr.com/ru/articles/1/"
    assert absolutize("https://x.y/z") == "https://x.y/z"
    assert absolutize(None) is None
    assert absolutize("") is None


def test_format_datetime_uses_given_timezone():
    moment = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
    msk = timezone(timedelta(hours=3))
    assert format_datetime(moment, msk) == "16 окт 2026 в 12:30"
    assert format_date(moment, msk) == "16 окт 2026"
    assert format_datetime(None) == ""
    assert format_date(None) == ""
    # Naive datetimes are not shifted.
    assert format_datetime(datetime(2026, 5, 1, 8, 5)) == "1 мая 2026 в 08:05"
    assert format_date(datetime(2026, 1, 2)) == "2 янв 2026"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "0"),
        (0, "0"),
        (999, "999"),
        (1000, "1K"),
        (1290, "1.3K"),
        (25905, "26K"),
        (1_500_000, "1.5M"),
        (-2500, "-2.5K"),
    ],
)
def test_format_count(value, expected):
    assert format_count(value) == expected


def test_format_score_and_number():
    assert format_score(43) == "+43"
    assert format_score(-2) == "-2"
    assert format_score(0) == "0"
    assert format_score(None) == "0"
    assert format_score(1.5) == "+1.5"
    assert format_number(None) == "—"
    assert format_number(116) == "116"
    assert format_number(43.0) == "43"
    assert format_number(467.81) == "467.81"
    assert format_number(12.50) == "12.5"


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (1, "комментарий"),
        (2, "комментария"),
        (5, "комментариев"),
        (11, "комментариев"),
        (21, "комментарий"),
        (104, "комментария"),
    ],
)
def test_plural(n, word):
    assert plural(n, "комментарий", "комментария", "комментариев") == word


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 с"),
        (40.4, "40 с"),
        (60, "1 мин"),
        (460, "7 мин 40 с"),
        (3600, "1 ч"),
        (3900, "1 ч 5 мин"),
        (-5, "0 с"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(30, "меньше минуты"), (60, "примерно 1 мин"), (61, "примерно 2 мин"), (3600, "примерно 1 ч"),
     (4800, "примерно 1 ч 20 мин")],
)  # fmt: skip
def test_format_eta(seconds, expected):
    assert format_eta(seconds) == expected


def test_format_short_datetime():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    assert format_short_datetime(None) == ""
    assert (
        format_short_datetime(datetime(2026, 10, 16, 9, 30, tzinfo=UTC), UTC, now)
        == "16 окт, 09:30"
    )
    assert format_short_datetime(datetime(2025, 3, 5, 9, 30, tzinfo=UTC), UTC, now) == "5 мар 2025"
    assert format_short_datetime(datetime(2026, 1, 2, 3, 4), now=now) == "2 янв, 03:04"
