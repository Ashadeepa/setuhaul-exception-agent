"""Load a .env file from the project root, if present. No extra dependency.

Values already set in the environment win. Empty values are skipped, so a blank
UPSTASH_REDIS_REST_URL= keeps memory in SQLite.
"""
import os
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)
