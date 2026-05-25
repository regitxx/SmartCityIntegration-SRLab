"""smcity — HK Smart City agent for the Lab of Social Robotics."""

from __future__ import annotations

import tomllib
from pathlib import Path


def _read_version_from_pyproject() -> str:
    """Single source of truth: derive the package version from pyproject.toml.

    Prevents the v0.5.x drift bug where this `__init__.py` and
    `pyproject.toml` had to be bumped in lockstep but only one of them
    actually got updated, leaving `/health` reporting a stale version
    long after the release shipped.

    The runtime Docker image copies `pyproject.toml` into `/app/` (see
    Dockerfile) so the lookup works in both dev and production.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (FileNotFoundError, KeyError, OSError):
        return "0.0.0+unknown"


__version__ = _read_version_from_pyproject()
