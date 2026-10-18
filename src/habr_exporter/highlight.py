"""Syntax highlighting of code blocks.

Habr highlights code in the browser (highlight.js) based on the ``<code class="python">`` class.
Here the same is done with Pygments, and the result is returned in a neutral form, a list of styled
fragments, so both the HTML and DOCX renderers can use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pygments import lex
from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.token import _TokenType
from pygments.util import ClassNotFound

STYLE_NAME = "xcode"

# highlight.js classes whose names differ from Pygments aliases.
_ALIASES = {
    "plaintext": None,
    "text": None,
    "nohighlight": None,
    "shell": "bash",
    "sh": "bash",
    "zsh": "bash",
    "console": "console",
    "cs": "csharp",
    "c#": "csharp",
    "js": "javascript",
    "ts": "typescript",
    "golang": "go",
    "1c": None,
    "yml": "yaml",
    "dockerfile": "docker",
    "objectivec": "objective-c",
    "vbnet": "vb.net",
    "pgsql": "postgresql",
    "xml": "xml",
    "html": "html",
}


@dataclass(frozen=True, slots=True)
class Span:
    """Styled code fragment. ``color`` is hex without ``#``, or ``None``."""

    text: str
    color: str | None = None
    bold: bool = False
    italic: bool = False


@lru_cache(maxsize=64)
def _lexer_for(language: str) -> Lexer | None:
    name = language.strip().lower()
    if name in _ALIASES:
        mapped = _ALIASES[name]
        if mapped is None:
            return None
        name = mapped
    try:
        return get_lexer_by_name(name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def language_from_classes(classes: str | None) -> str | None:
    """Extract the language from a ``class`` attribute (``"python"``, ``"language-go hljs"``)."""
    for raw in (classes or "").split():
        name = raw.removeprefix("language-").removeprefix("lang-")
        if name and name not in {"hljs", "code"}:
            return name
    return None


@lru_cache(maxsize=256)
def _style_for(token: _TokenType) -> tuple[str | None, bool, bool]:
    style = get_style_by_name(STYLE_NAME)
    # Some lexers (YAML: Token.Literal.Scalar.Plain) emit tokens missing from the style table,
    # and style_for_token() fails on them, so use the nearest known parent token.
    while True:
        try:
            info = style.style_for_token(token)
        except KeyError:
            if token.parent is None:  # pragma: no cover - the root Token is always in the table
                return None, False, False
            token = token.parent
        else:
            return info["color"] or None, bool(info["bold"]), bool(info["italic"])


def highlight(code: str, language: str | None) -> list[Span]:
    """Split code into colored fragments. Without a language, return one uncolored fragment."""
    lexer = _lexer_for(language) if language else None
    if lexer is None:
        return [Span(code)] if code else []

    spans: list[Span] = []
    for token, value in lex(code, lexer):
        if not value:
            continue
        color, bold, italic = _style_for(token)
        prev = spans[-1] if spans else None
        if prev and (prev.color, prev.bold, prev.italic) == (color, bold, italic):
            spans[-1] = Span(prev.text + value, color, bold, italic)
        else:
            spans.append(Span(value, color, bold, italic))
    return spans
