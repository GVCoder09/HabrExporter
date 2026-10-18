"""Entry point for the PyInstaller build: runs the GUI like the ``habr-exporter`` command."""

import sys

from habr_exporter.app import main

if __name__ == "__main__":
    sys.exit(main())
