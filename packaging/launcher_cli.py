"""Entry point for the PyInstaller build: runs the CLI like the ``habr-exporter-cli`` command."""

import sys

from habr_exporter.cli import main

if __name__ == "__main__":
    sys.exit(main())
