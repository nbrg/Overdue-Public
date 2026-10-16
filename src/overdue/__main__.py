"""Enable ``python -m overdue`` (a shorter alias for ``python -m overdue.cli``)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
