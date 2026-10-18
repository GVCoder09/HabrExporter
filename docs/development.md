# Development

## Environment

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -e ".[dev]"
```

Development dependencies: `pytest`, `pytest-cov`, `pytest-qt`, `ruff`.

## Tests

```bash
pytest
```

```bash
pytest --cov --cov-report=term-missing
```

```bash
pytest -m network
```

* Only offline tests run by default (`-m "not network"` in `pyproject.toml`).
* `pytest -m network` downloads a real article from habr.com and exports it to every format.
* The coverage threshold is 85% (`[tool.coverage.report] fail_under`).
* Qt tests run without windows: `conftest.py` sets `QT_QPA_PLATFORM=offscreen`. It also imports
  Qt WebEngine before `QApplication` is created, as PDF printing requires.

### Fixtures

`tests/fixtures/*.json` are synthetic Habr API responses with the same structure as the real site.
The article contains every supported construct (nested lists, tables with `colspan`, an SVG formula,
a lazy-loaded WebP, a broken image, an iframe, a spoiler, junk `script`/`code-explainer`, and so on);
the comments cover threads, a deleted comment and an "orphaned" reply.

`conftest.FakeSession` replaces `requests.Session` with a "URL -> response" table. Request parameters
other than the language ones (`fl`/`hl`) are part of the URL as a query string with sorted keys; for
publication lists use `conftest.listing_url()`. A response may be a function: this is how cancellation
tests hold the worker thread. The `batch_session` fixture adds two more articles and bookmarks of user
`alice` on two list pages.

### Test layout

Test files are named after the module they cover (`test_parser.py`, `test_batch.py`, `test_i18n.py`…).
The less obvious ones:

| File | What it checks |
|---|---|
| `test_exporters.py` | format order and beta labels; HTML, DOCX (including **OOXML element order**) and PDF |
| `test_ebooks.py` | EPUB and FB2, including FB2 schema rules |
| `test_json_export.py`, `test_markdown_export.py` | the JSON and Markdown formats, with their CLI and batch scenarios |
| `test_gui.py`, `test_gui_features.py` | the window and the "Batch" tab; themes, widgets, clipboard, `system.py` |
| `test_network.py` | live habr.com (only with `-m network`) |

## Linter and formatting

```bash
ruff check src tests
```

```bash
ruff format src tests
```

The rules are set in `pyproject.toml`. The line length is 100.
Cyrillic in strings is allowed: UI texts and test data are in Russian.

## Code style

* Comments, docstrings and documentation are in English. Comments explain why, not what: do not
  restate the code.
* Docstrings follow PEP 257 (`[tool.ruff.lint.pydocstyle] convention = "pep257"`): a one-line summary
  ending with a period, in the imperative mood for functions ("Return…", "Save…"), then a blank line
  and the details if needed.

## How to add a new format

1. Create `exporters/<format>.py` with a class that has `extension`, `title` and
   `export(article, path, options, images) -> Path`.
2. Add a value to `ExportFormat` (`exporters/base.py`) and to `get_exporter()`. The order of the enum
   values is the order of the buttons in the window. A new format should go into `_BETA` first.
3. Add the `format.hint.<format>` (tooltip) and `format.short.<format>` (the line on the tile) texts
   to `i18n/ru.py` and `i18n/en.py`. The format appears in the CLI and in batch mode by itself. If the exporter needs Qt (for example, to convert SVG to PNG),
   add the format to `_NEEDS_QT`: then the CLI creates a `QApplication`. If the format does not store
   images inside the file (like JSON), adjust `ExportFormat.embeds_images` so they are not downloaded.
4. If HTML suits the format, use `render_html.render_article()` with your own `image_src` function.
5. Write tests in `tests/test_exporters.py`.

## If Habr changed its API

First run `pytest -m network`. If it fails, compare a fresh response from
`https://habr.com/kek/v2/articles/<id>/` with `tests/fixtures/article.json` and fix
`parser.py`. If only the structure changed, the network code does not need to be touched.
