"""A local copy of every document / page we retrieve, named by its SHA-256.

This lets the AI extractors work only on material that was already
retrieved (never fetching anything new), and records exactly which bytes
the model was shown: sources.content_sha256 is the fingerprint.

Files live in document_cache/ in the project folder (git-ignored).
"""

import hashlib
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "document_cache"


def save(provider, content, suffix):
    """Store bytes; returns (relative path, sha256)."""
    sha = hashlib.sha256(content).hexdigest()
    folder = CACHE_DIR / provider
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha[:24]}{suffix}"
    if not path.exists():
        path.write_bytes(content)
    return path.relative_to(CACHE_DIR.parent).as_posix(), sha


def load(relative_path):
    """Return the cached bytes, or None if the file is missing."""
    path = CACHE_DIR.parent / relative_path
    return path.read_bytes() if path.exists() else None
