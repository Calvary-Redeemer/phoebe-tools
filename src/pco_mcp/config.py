"""Configuration: load PCO credentials from the environment / .env file.

Planning Center Personal Access Tokens authenticate with HTTP Basic auth, where
the App ID is the username and the Secret is the password. In this project those
are stored as PCO_CLIENT_ID and PCO_CLIENT_PAT.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_URL = "https://api.planningcenteronline.com/services/v2"

# Load the .env that sits at the project root (two levels up from this file:
# src/pco_mcp/config.py -> project/). load_dotenv is a no-op if the file is
# absent, so real environment variables still take precedence in production.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    app_id: str
    secret: str
    base_url: str = BASE_URL


def load_settings() -> Settings:
    """Read credentials from the environment, raising a clear error if missing."""
    app_id = os.environ.get("PCO_CLIENT_ID")
    secret = os.environ.get("PCO_CLIENT_PAT")
    missing = [
        name
        for name, value in (("PCO_CLIENT_ID", app_id), ("PCO_CLIENT_PAT", secret))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing Planning Center credentials: "
            + ", ".join(missing)
            + ". Set them in the environment or in the project .env file "
            "(see .env.example)."
        )
    assert app_id and secret  # narrows types for the checker
    return Settings(app_id=app_id, secret=secret)
