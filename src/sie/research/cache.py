"""A file cache for fetched pages: one entry per URL, one blob per distinct content.

Entries are written once and atomically (temp file, then rename); a blob is never rewritten, so what was
fetched stays exactly as received (AGENTS.md rule 4). Reading verifies the blob against its recorded hash
and fails loudly on a mismatch. No database table and no migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sie.evidence.errors import EvidenceError, FetchRefused
from sie.evidence.web import FetchedPage, WebFetcher, check_url


def url_key(url: str) -> str:
    clean, _, _ = check_url(url)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class FetchCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _entry(self, key: str) -> Path:
        return self.root / "index" / f"{key}.json"

    def _blob(self, sha: str) -> Path:
        return self.root / "blobs" / f"{sha}.bin"

    def put(self, page: FetchedPage) -> str:
        """Store a page; returns its content hash. The same content is stored once."""
        sha = hashlib.sha256(page.body).hexdigest()
        blob = self._blob(sha)
        if not blob.exists():
            _atomic_write(blob, page.body)
        meta = {
            "requested_url": page.requested_url,
            "final_url": page.final_url,
            "status": page.status,
            "content_type": page.content_type,
            "retrieved_at": page.retrieved_at.isoformat(),
            "robots_checked_at": page.robots_checked_at.isoformat(),
            "notes": list(page.notes),
            "sha256": sha,
        }
        _atomic_write(
            self._entry(url_key(page.requested_url)),
            json.dumps(meta, sort_keys=True).encode("utf-8"),
        )
        return sha

    def get(self, url: str) -> FetchedPage | None:
        """The cached page for a URL whatever its age, or None. Raises when the blob was altered."""
        entry = self._entry(url_key(url))
        if not entry.exists():
            return None
        meta = json.loads(entry.read_text(encoding="utf-8"))
        blob = self._blob(meta["sha256"])
        if not blob.exists():
            raise EvidenceError(f"cache entry for {meta['requested_url']} lost its content")
        body = blob.read_bytes()
        if hashlib.sha256(body).hexdigest() != meta["sha256"]:
            raise EvidenceError(
                f"cache content for {meta['requested_url']} no longer matches its hash"
            )
        return FetchedPage(
            meta["requested_url"],
            meta["final_url"],
            meta["status"],
            meta["content_type"],
            body,
            datetime.fromisoformat(meta["retrieved_at"]),
            datetime.fromisoformat(meta["robots_checked_at"]),
            tuple(meta["notes"]),
        )


@dataclass(frozen=True)
class Fetched:
    page: FetchedPage
    from_cache: bool
    stale: bool = False  # a cached copy older than allowed, used because the live fetch failed
    reason: str = ""  # why the cache was used or the live fetch failed


class CachedFetcher:
    """Cache first, then the safe fetcher. A failed live fetch falls back to the cached copy, flagged.

    The only thing this class can reach is the injected ``WebFetcher``; there is no other endpoint.
    """

    def __init__(
        self,
        fetcher: WebFetcher,
        cache: FetchCache,
        *,
        max_age: timedelta,
        now=lambda: datetime.now(UTC),
        use_stale_on_failure: bool = True,
    ) -> None:
        self.fetcher, self.cache, self.max_age = fetcher, cache, max_age
        self._now, self._stale_ok = now, use_stale_on_failure

    def allowed(self, url: str) -> bool:
        return self.fetcher.allowed(url)

    def fetch(self, url: str) -> Fetched:
        cached = self.cache.get(url)
        if cached is not None and self._now() - cached.retrieved_at <= self.max_age:
            return Fetched(cached, True, False, "fresh cache hit")
        try:
            page = self.fetcher.fetch(url)
        except FetchRefused as exc:
            if cached is not None and self._stale_ok:
                return Fetched(
                    cached, True, True, f"live fetch failed ({exc.code}); using the cached copy"
                )
            raise
        self.cache.put(page)
        return Fetched(page, False, False, "fetched live")
