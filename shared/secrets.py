"""Read one server's credentials from its own file in /opt/secrets.

Each tool server is started by Hermes with a single environment variable pointing at
its secrets file (for example PCO_ENV_FILE=/opt/secrets/pco.env). The server reads only
that file, so one server never sees another server's keys, and no secret ever has to be
pasted into Hermes's config.yaml.
"""
from __future__ import annotations

import os
from pathlib import Path


class MissingSecret(RuntimeError):
    """A required credential is not configured."""


def load_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Parse a KEY=VALUE file. Blank lines and # comments are ignored; surrounding quotes are stripped."""
    values: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def require(names: list[str], file_var: str) -> dict[str, str]:
    """Return the named credentials, read from the file named in env var `file_var`.

    Falls back to plain environment variables of the same names (handy for local tests).
    Raises MissingSecret naming what is missing, never the values.
    """
    source: dict[str, str] = {}
    path = os.environ.get(file_var)
    if path:
        source = load_env_file(path)
    found = {n: source.get(n) or os.environ.get(n, "") for n in names}
    missing = [n for n, v in found.items() if not v]
    if missing:
        where = path or f"environment (set {file_var} to point at the secrets file)"
        raise MissingSecret(f"Missing {', '.join(missing)} in {where}")
    return found
