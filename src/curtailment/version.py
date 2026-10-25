"""Version string for the status page: git describe if run from a checkout."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path


@lru_cache(maxsize=1)
def get_version() -> str:
    """`git describe` of the source tree, else the installed package version."""
    src = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            # safe.directory: the service user usually doesn't own the checkout
            ["git", "-c", "safe.directory=*", "describe", "--always", "--dirty", "--tags"],
            cwd=src, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return metadata.version("solar-curtailment-daemon") + " (no git)"
    except metadata.PackageNotFoundError:
        return "unknown"
