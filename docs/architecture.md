# Architecture

## Data flow

```
link ──► url.parse_article_id ──► HabrClient (JSON API) ──► parser.parse_article ──► Article
                                                                                     │
                       ImageStore.fetch_all (if images are embedded) ◄───────────────┤
                                                                                     ▼
            exporters: PdfExporter / HtmlExporter / DocxExporter / … / JsonExporter ──► file
```

Networking, parsing, rendering and the GUI are separated. The models (`models.py`) are plain
dataclasses that depend on neither the network nor Qt, so the renderers are tested on synthetic data.

## Modules

| Module | Purpose |
|---|---|
| `url.py` | Extracts the article number from any Habr link (including old `habrahabr.ru` ones) |
| `client.py` | `HabrClient`: `requests.Session` with retries (429/5xx), timeouts, `HabrError` / `ArticleNotFoundError` |
| `parser.py` | JSON -> models; `clean_html()` normalizes article and comment HTML; `parse_comments()` builds the tree |
| `models.py` | `Article`, `User`, `Company`, `Comment`, `Hub`, `Statistics` |
| `images.py` | `ImageStore`: parallel downloading and caching, MIME detection, `raster_for_docx()` (WebP/SVG -> PNG) |
| `highlight.py` | Pygments code highlighting as `Span`s understood by both HTML and DOCX |
| `formatting.py` | Dates, counters, rating, plural forms |
| `i18n/` | UI languages (`ru.py`, `en.py`), `tr()` / `trn()`, `Bindings` for translating the window on the fly |
| `render_html.py`, `assets/habr.css` | A standalone Habr-styled HTML page: the basis for HTML, PDF and the preview |
| `exporters/` | One module per format plus `base.py` (`ExportFormat`, `ExportOptions`, `Exporter` protocol, `collect_image_urls()`, `section_headings()`) |
| `service.py` | Top-level scenarios: `load_article()`, `download_images()`, `export_article()`, `suggest_filename()` |
| `batch.py` | Batch export: `list_articles()`, `run_batch()`, `index.html` |
| `gui/` | `MainWindow` ("Single article" tab), `BatchPanel` ("Batch" tab), `TaskThread` for background work, theme and widgets |
| `app.py`, `cli.py` | Entry points: GUI and command line |

## Where the data comes from

The Habr site is an SPA and gets all article data from JSON endpoints. Parsing JSON is more reliable
than parsing HTML markup, which changes regularly.

| Request | What we take |
|---|---|
| `GET /kek/v2/articles/{id}/` | the article itself: text, author, date, hubs, tags, statistics |
| `GET /kek/v2/articles/{id}/comments/` | the flat `comments` dictionary and the `threads` order |
| `GET /kek/v2/users/{alias}/card/` | author card: karma, rating, followers… |
| `GET /kek/v2/companies/{alias}/card/` | company card: logo, rating, description, contacts |

Batch mode needs a user's publication lists (verified on live habr.com in October 2026):

| Request | What it returns |
|---|---|
| `GET /kek/v2/articles/?user={alias}&user_bookmarks=true&page={N}&perPage=100` | the user's public bookmarks |
| `GET /kek/v2/articles/?user={alias}&page={N}&perPage=100` | the author's articles (without news) |
| `GET /kek/v2/articles/?user={alias}&news=true&page={N}&perPage=100` | the author's news |

The response contains `pagesCount`, `publicationIds` (newest first) and `publicationRefs` (`id` ->
short data). `perPage` is capped at 100 (422 above that). A page past the last one and an empty list
are a 404 with `"message": "Page is out of range"`; an unknown username is a 404 too.
`HabrClient` keeps the message in `ArticleNotFoundError.api_message`, and `batch.py` uses it to tell
an empty list from an error. Bookmarks may also contain `megaproject` entries; they load through the
same `/articles/{id}/`.

The company is the hub with `type == "corporative"`. The author and company cards are optional, and
so are comments: if a request fails, the article is still saved, just without that block.

### HTML sanitizing (`clean_html`)

* removes service blocks (`code-explainer`, `script`, `style`) and the xhtml wrapper `div`;
* resolves lazy loading (`data-src` -> `src`) and makes relative links absolute;
* replaces `iframe`s (YouTube, VK Video, widgets) with a link: a player cannot be saved offline.

### Comment tree

The order of root threads comes from `threads` and of replies from `children`, as on the site. A reply
known only by its own `parentId` still goes to its parent; comments with no place at all are appended
by time. `level` is recomputed from the actual depth. Deleted and blocked comments are shown with the
caption "НЛО прилетело…", as on Habr.

## Exporters

All exporters share the same interface:

```python
export(article, path, options: ExportOptions, images: ImageStore | None) -> Path
```

### Image modes

`render_html.render_article()` takes an `image_src(url) -> str | None` function that decides what goes
into `src`: the original URL, a `data:` URI, or `None`, in which case a link to the image is shown.

| Format | `EMBED` | `LINK` |
|---|---|---|
| PDF | `data:` URI | placeholder link |
| HTML | `data:` URI; the original URL if the download failed | original URLs |
| DOCX | `run.add_picture()`; WebP and SVG are converted to PNG | placeholder hyperlink |
| EPUB | files in `images/` inside the book | placeholder link |
| FB2 | `<binary>`, PNG and JPEG only | placeholder link |
| Markdown | `data:` URIs in `[img-N]: …` definitions at the end of the file | original URLs |
| JSON | not embedded: URLs only, in `images` | same |

Images are downloaded only if `ExportOptions.downloads_images` is set: `EMBED` mode and a format
that can store them (`ExportFormat.embeds_images`; JSON cannot).

### PDF (`exporters/pdf.py`)

HTML is printed by Qt WebEngine (`QWebEnginePage.printToPdf`), so the PDF looks like the site. The
page goes through a temporary file (`setHtml()` is limited to 2 MB) and is loaded with JavaScript
disabled.

WebEngine objects live only in the GUI thread:

* `PdfPrinter` is asynchronous (`finished` / `failed` signals); the GUI uses it;
* `html_to_pdf()` is a synchronous wrapper with a local `QEventLoop` for the CLI and tests;
* `PdfExporter.render()` only prepares the HTML and is thread-safe, so the GUI calls it in the
  background and starts printing in the main thread.

### HTML (`exporters/html.py`)

The page from `render_article()` is saved as is: styles in `<style>`, no JavaScript, no external
dependencies, images as `data:` URIs in `EMBED` mode. MHTML was dropped because Firefox and Safari
cannot open it.

### DOCX (`exporters/docx.py`)

`DocxBuilder` walks the HTML (lxml) and builds the document with python-docx. Text and inline tags
accumulate in the current paragraph and a block tag closes it; an immutable `Fmt` (formatting) and a
`Ctx` (indent, quote, thread line) are passed down the tree. Lists are drawn manually (a bullet or
number plus a hanging indent) because Word numbering "glues" adjacent lists together. Hyperlinks are
built from raw `w:hyperlink` elements (python-docx has no API for them).

Word strictly checks the order of child elements in `w:pPr`, `w:rPr`, `w:tblPr`, `w:tcPr`, so shading
and borders are inserted with `insert_element_before()` and an explicit list of the following
elements. `test_docx_schema_order` guards this.

### EPUB (`exporters/epub.py`, beta)

EPUB 3 with a fallback `toc.ncx` for EPUB 2 readers. It reuses the same page blocks as HTML and PDF;
`_sanitize()` brings them to strict XHTML (drops scripts, iframes and invalid attributes and ids,
replaces obsolete tags, expands `<details>`). EPUB does not allow remote images, so in `LINK` mode and
for images that failed to download a link is shown. The table of contents is built from the article's
top-level headings (`section_headings()`), followed by "About the author" and "Comments".

### FB2 (`exporters/fb2.py`, beta)

`Fb2Builder` walks the HTML like `DocxBuilder` and builds a FictionBook 2.0 tree. FB2 is poor in
structure, so some things are emulated:

* no lists: items are paragraphs with a bullet or number and non-breaking-space indent;
* no code blocks: each line is `<p><code>`;
* `<cite>` cannot be nested, so a comment reply is a section-level `<cite>` with a "↳" depth marker
  and a "reply to @username" note;
* avatars and the logo are omitted because readers stretch them to the full screen.

Top-level headings open a `<section>`, smaller ones become `<subtitle>`.
`tests/test_ebooks.py::_check_fb2_structure` checks the schema rules that are easy to break.

### Markdown (`exporters/markdown.py`, beta)

`MarkdownConverter` walks the sanitized HTML and produces CommonMark with GitHub extensions (tables,
`<details>` for spoilers, `$…$` for formulas). The non-obvious parts:

* text escaping is context-aware (line starts that look like a heading, list or quote; `_` only at
  word boundaries so `snake_case` stays as is);
* whitespace at the edges of `<strong>`/`<em>` is moved outside the markers (`** a **` is not emphasis);
* code fences are longer than any backtick run inside the code;
* replies in comments are nested as `>` quotes no deeper than `MAX_QUOTE_DEPTH`;
* the file is UTF-8 with LF line endings on every OS.

### JSON (`exporters/json.py`)

A machine-readable version of the article. `build_document()` builds a dictionary, and `JsonExporter`
writes it in UTF-8 with `ensure_ascii=False`, `indent=2` and LF line endings.

The schema is `habr-exporter/article`, version `1`. On incompatible changes (renaming or removing a
field, changing a type) `version` is incremented; new fields are added without changing it, so
unknown keys should be ignored.

General rules: dates are ISO 8601 strings as the API returns them (`ExportOptions.tz` does not affect
them); a missing value is `null`; empty strings stay empty.

```jsonc
{
  "schema": "habr-exporter/article",
  "version": 1,
  "exported_at": "2026-10-16T12:00:00+00:00",   // time of saving, UTC
  "source_url": "https://habr.com/ru/articles/1000001/",
  "article": {
    "id": "1000001",                              // a string, as in the API
    "url": "…",
    "title": "…",
    "lang": "ru",                                 // ru | en
    "post_type": "article",                       // article | news | …
    "published": "2026-03-05T09:30:00+00:00",     // or null
    "reading_time": 7,                            // minutes or null
    "complexity": "medium",                       // low | medium | high | null
    "is_translation": true,
    "original_author": "Jane Doe",                // for translations, otherwise null
    "original_url": "https://example.com/original",
    "hubs": [{"alias": "python", "title": "Python", "url": "…", "is_corporative": false, "is_profiled": true}],
    "tags": ["python"],
    "statistics": {"score": 17, "votes": 19, "votes_plus": 18, "votes_minus": 1,
                   "views": 15400, "favorites": 21, "comments": 7},
    "author": {"alias": "…", "fullname": "…", "karma": 51, "rating": 12.5, "url": "…"},  // all models.User fields
    "company": {"alias": "acme", "title": "ACME", "rating": 467.81, "url": "…"},        // all models.Company fields, or null
    "body_html": "<p>…</p>",                      // sanitized HTML (parser.clean_html)
    "body_text": "Абзац\n\nАбзац",                // plain text
    "images": [{"url": "…", "alt": "x^2"}, {"url": "…"}],
    "comments": [                                 // null if comments were not saved
      {"id": "1", "parent_id": null, "level": 0,
       "author": {"alias": "alice", "fullname": "Алиса", "avatar_url": "…"},   // null for deleted ones
       "published": "…", "edited": null, "score": 5, "votes": 5,
       "is_post_author": false, "deleted": false, "html": "<p>…</p>", "text": "…"}
    ]
  }
}
```

* **`body_text` and `text`** come from `html_to_text()`: blocks are separated by a blank line, list
  items get "•" or a number, table cells are separated by tabs, a formula becomes its TeX source,
  other images are skipped.
* **`images`** are the URLs from `collect_image_urls()` without duplicates: the body, comments and
  avatars, the company logo. `alt` is present only if non-empty. Images are not embedded: base64
  bloats the file.
* **`comments`** is a flat list in display order (`Article.iter_comments()`, depth-first). A parent
  always comes before its reply, so the tree is restored from `parent_id` in a single pass. Deleted
  comments have `deleted: true`, `author: null` and empty `html`/`text`.
* **`complexity`**: the model stores a human-readable label, JSON stores the API value.

## Batch export (`batch.py`)

```
BatchSource ──► list_articles ──► [ArticleRef…] ──► run_batch ──► files + index.html
 (bookmarks,    (list pages        (number, link,       │
  author, file)  or file lines)     title, author)      └─► for each article: load_article → export_article
```

* **List.** `list_articles()` collects the whole list first so progress can be "N of M". With
  `limit`, no more pages are requested once there are enough articles. For an author, articles come
  first, then news. Duplicates are dropped.
* **Order.** `newest_first()` sorts the list by `timePublished`, newest first, undated at the end.
  `limit` is applied before sorting, to Habr's order, so not every page has to be downloaded. Links
  from a file keep line order.
* **Names and skipping.** The file is named `"{number} - {suggest_filename()}"`. `scan_existing()`
  finds such files in the folder, and those articles are skipped before any request. The format is
  taken into account: an HTML version does not prevent saving a PDF.
* **Atomicity.** An article is written to `~{name}` and renamed on success; on an error or Ctrl+C the
  temporary file is deleted.
* **Errors.** An exception while saving one article becomes `BatchItem(status=FAILED)` and the export
  continues. Only a failure to get the list raises `HabrError`.
* **Throttling.** `run_batch(delay=…)` enables `HabrClient.throttled(delay)`, a thread-safe minimum
  delay between JSON API requests (images from habrastorage.org are not throttled).
* **Cancellation and progress.** `cancelled()` is checked before each article and while collecting the
  list; `on_item(item, n, total)` reports each article.
* **PDF.** Without `print_pdf`, PDFs are printed synchronously with `html_to_pdf()` (CLI). The GUI
  passes `MainThreadPdfPrinter.print_html`: the worker thread lays out the HTML, sends it by signal to
  the GUI thread and waits until `PdfPrinter` finishes, so WebEngine is never touched from another
  thread.
* **Index.** `render_index()` builds a standalone page without JavaScript: the articles of this run,
  a "Failed to save" block and "Other articles in the folder" from earlier runs.

## GUI (`gui/`)

### Theme

* The look is defined by the window's stylesheet (QSS). Widgets are selected by `objectName` and
  dynamic properties (`role`, `kind`, `tone`, `variant`, `focused`). A property is changed with
  `widgets.set_prop()` because Qt does not recompute the style by itself. Property names must not
  clash with `QWidget` properties (`size`, `focus`…): `setProperty()` silently ignores them.
* `theme.apply(window, theme)` sets the palette and stylesheet. Icons and the toggle are custom-painted
  and take their colors from `theme.of(widget)`.
* "System" mode follows `QStyleHints.colorScheme()` and the `colorSchemeChanged` signal. The choice is
  stored in `QSettings`.
* `app.py` enables the Fusion style so that menus, dialogs and scroll bars look the same on every OS.
* The settings columns are `ScrollColumn`s: the window minimum (960×600) is smaller than the cards
  need, so without scrolling Qt would squeeze widgets below their minimum size.
* The preview is always light: it is a page in Habr styles.

### Language

* Texts are plain dictionaries `i18n/ru.py` and `i18n/en.py` with the same keys. `tr(key, name=...)`
  substitutes `{name}` placeholders, `trn(key, n)` picks a plural form. A missing translation falls
  back to Russian, then to the key. `tests/test_i18n.py` checks that keys and placeholders match.
* The language is process-wide (`i18n.set_language()`), so core messages (loading progress, network
  errors) are translated too. The CLI always uses English during `cli.main()`. Saved files are not
  translated: they are Habr content, not UI.
* By default the language follows the system locale. The choice is stored in `QSettings`.
* Translation on the fly: widgets are created through `Bindings` (`self.t.text(widget, key)`…), which
  remember how to set the text; `MainWindow.apply_language()` replays them, and state-dependent texts
  are recomputed from data.

### "Single article"

* Networking, image downloading and document assembly run in a `TaskThread` (`QThread`); results and
  errors arrive by signal in the GUI thread.
* Downloaded images are cached in the window, so saving again in another format does not download
  them again.
* If the clipboard holds a Habr article link and the field is empty, a "Paste and load" offer
  appears (`system.article_url_from_clipboard()`).
* Format tiles follow `ExportFormat` order, with a "beta" badge. For a format that does not store
  images (JSON) the image mode choice is disabled.
* Settings are stored in `QSettings`. An unknown saved value (e.g. `mhtml` from an old version) is
  replaced with the default.
* For tests the window has the `loaded`, `saved` and `error` signals, an `export_to()` method without
  dialogs and a `show_error_dialogs` flag.

### "Batch"

* `BatchPanel` runs `run_batch()` in a single `TaskThread`. `item_started` / `item_done` are emitted
  in the worker thread and delivered to the GUI thread through the queue; the result is
  `finished(BatchReport)`.
* The username is shared by "Bookmarks" and "Author's articles"; the links file path is stored
  separately (`batch/user`, `batch/file`). The legacy `batch/value` is split by meaning on first run
  (`_looks_like_file()`). A link in the file field or a path in the username field disables "Start"
  and shows a hint.
* Panel states (`_show_state`): `idle`, `running`, `done`, `error`.
* The log is a `QListWidget` of `LogRow`s, newest first. The row of the article being downloaded shows
  its stage and turns into the final one. Failed rows have "Retry", which calls `batch.retry_item()`
  in a worker thread, replaces the result in the report and rewrites `index.html`.
* The time remaining estimate is the average over the last 10 downloaded articles (skipped ones do
  not count) times the number remaining.
* While an export is running, the "Single article" tab is locked, and vice versa (`MainWindow.busy`).
  On close, `BatchPanel.shutdown()` cancels the export and releases the thread waiting for PDF
  printing.
