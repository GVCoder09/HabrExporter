"""Data models of a Habr article.

All models are plain dataclasses, immutable by convention, independent of both the network and
Qt, which makes the renderers easy to test.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Hub:
    """Hub (section) where the article is published."""

    alias: str
    title: str
    is_profiled: bool = False
    is_corporative: bool = False

    @property
    def url(self) -> str:
        kind = "companies" if self.is_corporative else "hubs"
        suffix = "articles/" if self.is_corporative else ""
        return f"https://habr.com/ru/{kind}/{self.alias}/{suffix}"


@dataclass(slots=True)
class User:
    """Habr user: the author of an article or a comment."""

    alias: str
    fullname: str = ""
    avatar_url: str | None = None
    speciality: str = ""
    karma: float | None = None
    karma_votes: int | None = None
    rating: float | None = None
    rating_position: int | None = None
    followers: int | None = None
    registered: datetime | None = None
    last_activity: datetime | None = None
    location: str = ""
    workplace: list[str] = field(default_factory=list)
    articles_count: int | None = None
    comments_count: int | None = None
    deleted: bool = False

    @property
    def url(self) -> str:
        return f"https://habr.com/ru/users/{self.alias}/"


@dataclass(slots=True)
class Contact:
    """Link from a company card (website, Telegram, etc.)."""

    title: str
    url: str


@dataclass(slots=True)
class Company:
    """Company whose blog the article is published in."""

    alias: str
    title: str
    description: str = ""
    logo_url: str | None = None
    rating: float | None = None
    subscribers: int | None = None
    site_url: str | None = None
    legal_name: str = ""
    location: str = ""
    staff: str = ""
    contacts: list[Contact] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://habr.com/ru/companies/{self.alias}/"


@dataclass(slots=True)
class Comment:
    """Comment with nested replies."""

    id: str
    author: User | None
    html: str
    published: datetime | None
    score: int = 0
    votes: int = 0
    level: int = 0
    edited: datetime | None = None
    is_post_author: bool = False
    deleted: bool = False
    children: list[Comment] = field(default_factory=list)

    def walk(self) -> Iterator[Comment]:
        """Walk the thread depth-first, starting with the comment itself."""
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(slots=True)
class Statistics:
    """Article counters."""

    score: int = 0
    votes: int = 0
    votes_plus: int = 0
    votes_minus: int = 0
    views: int = 0
    favorites: int = 0
    comments: int = 0


@dataclass(slots=True)
class Article:
    """Full article description together with its comments."""

    id: str
    url: str
    title: str
    body_html: str
    author: User
    published: datetime | None
    lang: str = "ru"
    post_type: str = "article"
    reading_time: int | None = None
    complexity: str | None = None
    hubs: list[Hub] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    statistics: Statistics = field(default_factory=Statistics)
    company: Company | None = None
    comments: list[Comment] = field(default_factory=list)
    is_translation: bool = False
    original_author: str | None = None
    original_url: str | None = None

    def iter_comments(self) -> Iterator[Comment]:
        """Yield all article comments in display order."""
        for root in self.comments:
            yield from root.walk()

    @property
    def comments_total(self) -> int:
        return sum(1 for _ in self.iter_comments())
