"""Enable `python -m smcity_fuzz ...` invocation."""

from __future__ import annotations

import sys

from smcity_fuzz.cli import main

if __name__ == "__main__":
    sys.exit(main())
