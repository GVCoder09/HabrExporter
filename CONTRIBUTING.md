# Contributing to Habr Exporter

Thank you for helping! Bug reports, ideas and pull requests are welcome.
By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and suggesting features

* Search [existing issues](https://github.com/GVCoder09/HabrExporter/issues) first.
* For a bug, include the article link, the format and options you used, what you expected and what
  happened, your OS and the output of `habr-exporter-cli --version`. Run the CLI without `-q` and
  attach its output: it shows where the export stopped.
* If an article that used to export fine no longer does, Habr may have changed its API. Mention it
  in the issue; `pytest -m network` (see below) confirms it.

Security problems are reported privately, see [SECURITY.md](SECURITY.md).

## Setting up the environment

Python 3.11 or newer is required.

```bash
git clone https://github.com/GVCoder09/HabrExporter.git
```

```bash
python -m venv .venv
```

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

On Windows, the last command is `.venv\Scripts\python -m pip install -e ".[dev]"`.

## Before opening a pull request

```bash
ruff check src tests
```

```bash
ruff format --check src tests
```

```bash
pytest --cov
```

* All three must pass: CI runs the same commands on Linux, Windows and macOS.
* Coverage must stay at 85% or higher. New code comes with tests: offline tests use the synthetic
  responses in `tests/fixtures` and `conftest.FakeSession`, never the real network.
* Tests that need live habr.com are marked `@pytest.mark.network` and run only with `pytest -m network`.
* Interface texts go into both dictionaries, `i18n/ru.py` and `i18n/en.py`; `tests/test_i18n.py`
  checks that keys and placeholders match. Saved files (article content) stay in Russian.
* Comments, docstrings and documentation are in English; see the
  [code style](docs/development.md#code-style).
* Add a line to the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

[docs/development.md](docs/development.md) explains the test suite, how to add an export format and
what to do when Habr changes its API. [docs/architecture.md](docs/architecture.md) describes how the
application is built.

## Pull requests

* One pull request per change; keep it focused.
* Describe what changed and why, and how you checked it. For UI changes, add a screenshot.
* Commit messages: a short summary line in the imperative mood ("Add FB2 table support"), then
  details if needed.

## Releases (maintainers)

1. Update `__version__` in `src/habr_exporter/__init__.py` and move the `[Unreleased]` notes in
   `CHANGELOG.md` under the new version.
2. Commit and push, then run the [Build workflow](.github/workflows/build.yml) manually (Actions ->
   Build -> Run workflow). It runs the tests and builds the wheel, the source archive and the
   Windows build, which are kept as artifacts of the run.
3. Create the GitHub release by hand and attach the downloaded files.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
