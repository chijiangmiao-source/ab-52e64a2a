"""Causal dot store for orbit maintenance rules.

Every ground-station write is a *dot* ``(site, count)`` carrying the rule
content plus a version-vector ``context`` describing its causal
predecessors.  The store maintains three guarantees the scheduling centre
relies on:

1. A write whose predecessors have not arrived yet is held as *pending* and
   never enters the effective projection.  It activates automatically
   (cascading) as soon as its predecessors arrive.
2. Two genuinely concurrent writes to the same rule key are both retained.
   A read returns either one deterministic value or an explicit conflict
   listing every maximal candidate with its dot and causal context; only
   candidates covered by another version vector are pruned.
3. A dot is immutable.  Re-delivering the exact same dot/content/context is
   idempotent; a dot that arrives twice with different content or a
   different context is equivocation and is rejected, as are counter
   jumps, unknown sites and self-contradictory vectors.

State is persisted with an atomic JSON snapshot after every mutation so
that accepted dots, pending writes and conflict candidates survive a
restart byte-for-byte.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional

SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/\-]{0,255}$")
MAX_CONTENT_BYTES = 64 * 1024
SNAPSHOT_NAME = "snapshot.json"
SNAPSHOT_VERSION = 1


class Rejected(Exception):
    """A write that must be explicitly refused (HTTP 400/409)."""

    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Record:
    """One accepted dot (effective or still pending)."""

    key: str
    site: str
    count: int
    content: str
    context: dict[str, int]

    @property
    def dot(self) -> tuple[str, int]:
        return (self.site, self.count)

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "site": self.site,
            "dot": [self.site, self.count],
            "content": self.content,
            "context": dict(self.context),
        }


def _validate_site(site: object, known: frozenset[str], *, where: str) -> str:
    if not isinstance(site, str) or not SITE_RE.match(site):
        raise Rejected(f"illegal site identifier in {where}")
    if site not in known:
        raise Rejected(f"unknown site in {where}: {site}")
    return site


class DotStore:
    """Thread-safe causal dot store with JSON snapshot persistence."""

    def __init__(
        self,
        stations: list[str] | set[str] | tuple[str, ...],
        data_dir: Optional[str] = None,
    ):
        known: set[str] = set()
        for raw in stations:
            site = raw.strip()
            if not SITE_RE.match(site):
                raise ValueError(f"illegal configured station: {raw!r}")
            known.add(site)
        if not known:
            raise ValueError("at least one known station is required")
        self._stations = frozenset(known)
        self._data_dir = data_dir
        # Every dot ever accepted, keyed by (site, count) -> Record.  Dots are
        # never removed: even causally obsolete ones anchor later contexts.
        self._effective: dict[tuple[str, int], Record] = {}
        self._pending: dict[tuple[str, int], Record] = {}
        self._key_dots: dict[str, set[tuple[str, int]]] = {}
        self._lock = threading.RLock()
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    @property
    def stations(self) -> frozenset[str]:
        return self._stations

    def _ancestors(self, rec: Record) -> set[tuple[str, int]]:
        """Transitive causal predecessors of an effective record."""
        seen: set[tuple[str, int]] = set()
        stack = [(s, c) for s, c in rec.context.items() if c >= 1]
        while stack:
            dot = stack.pop()
            if dot in seen:
                continue
            seen.add(dot)
            parent = self._effective.get(dot)
            if parent is not None:
                stack.extend(
                    (s, c) for s, c in parent.context.items() if c >= 1
                )
        return seen

    def _missing_deps(self, rec: Record) -> list[tuple[str, int]]:
        missing = [
            (s, c)
            for s, c in rec.context.items()
            if c >= 1 and (s, c) not in self._effective
        ]
        return sorted(missing)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def submit(
        self,
        key: str,
        site: str,
        count: object,
        content: object,
        context: object = None,
    ) -> dict:
        """Submit one station write.

        Returns ``{"state": "accepted"|"pending", "duplicate": bool, ...}``.
        Raises :class:`Rejected` for any malformed or equivocating write.
        """
        with self._lock:
            if not isinstance(key, str) or not KEY_RE.match(key):
                raise Rejected("illegal rule key")
            origin = _validate_site(site, self._stations, where="dot origin")

            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise Rejected("dot count must be a positive integer")
            if isinstance(content, str):
                if not content or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
                    raise Rejected("content must be 1..%d UTF-8 bytes" % MAX_CONTENT_BYTES)
            else:
                raise Rejected("content must be a string")

            ctx: dict[str, int] = {}
            if context is None:
                context = {}
            if not isinstance(context, dict):
                raise Rejected("context must be a version-vector object")
            for csite, cvalue in context.items():
                _validate_site(csite, self._stations, where="context")
                if isinstance(cvalue, bool) or not isinstance(cvalue, int) or cvalue < 0:
                    raise Rejected(f"context counter for {csite} must be >= 0")
                ctx[csite] = cvalue

            # A site's dots form one contiguous causal sequence: its own
            # predecessor dot (site, count-1) must be the observed counter.
            own = ctx.get(origin, 0)
            if own != count - 1:
                raise Rejected(
                    f"counter jump or self-contradictory vector for {origin}: "
                    f"dot count {count} but context carries {origin}:{own}"
                )

            dot = (origin, count)
            rec = Record(key=key, site=origin, count=count,
                         content=content, context=ctx)
            existing = self._effective.get(dot) or self._pending.get(dot)
            if existing is not None:
                same = (
                    existing.content == rec.content
                    and existing.context == rec.context
                )
                if not same:
                    raise Rejected(
                        f"equivocation rejected: dot [{origin},{count}] was "
                        "already submitted with different content or context",
                        status=409,
                    )
                state = "accepted" if dot in self._effective else "pending"
                result = {
                    "state": state,
                    "duplicate": True,
                    "dot": [origin, count],
                    "key": existing.key,
                }
                if state == "pending":
                    result["waiting_for"] = [
                        list(d) for d in self._missing_deps(existing)
                    ]
                return result

            missing = self._missing_deps(rec)
            if missing:
                self._pending[dot] = rec
                self._save()
                return {
                    "state": "pending",
                    "duplicate": False,
                    "dot": [origin, count],
                    "key": key,
                    "waiting_for": [list(d) for d in missing],
                }

            self._activate(rec)
            self._save()
            return {
                "state": "accepted",
                "duplicate": False,
                "dot": [origin, count],
                "key": key,
            }

    def _activate(self, rec: Record) -> None:
        """Move ``rec`` (and any now-satisfiable pending dots) into the
        effective projection."""
        activated = False
        self._effective[rec.dot] = rec
        self._key_dots.setdefault(rec.key, set()).add(rec.dot)
        activated = True
        while activated:
            activated = False
            for dot, pending in list(self._pending.items()):
                if not self._missing_deps(pending):
                    del self._pending[dot]
                    self._effective[dot] = pending
                    self._key_dots.setdefault(pending.key, set()).add(dot)
                    activated = True

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def read(self, key: str) -> dict:
        with self._lock:
            if not isinstance(key, str) or not KEY_RE.match(key):
                raise Rejected("illegal rule key")
            dots = self._key_dots.get(key, set())
            records = [self._effective[d] for d in dots]

            maximal: list[Record] = []
            for candidate in records:
                covered = False
                ancestors_of: dict[tuple[str, int], set[tuple[str, int]]] = {}
                for other in records:
                    if other.dot == candidate.dot:
                        continue
                    if other.dot not in ancestors_of:
                        ancestors_of[other.dot] = self._ancestors(other)
                    if candidate.dot in ancestors_of[other.dot]:
                        covered = True
                        break
                if not covered:
                    maximal.append(candidate)

            maximal.sort(key=lambda r: (r.site, r.count))
            pending_for = [
                (dot, rec) for dot, rec in self._pending.items() if rec.key == key
            ]
            pending_json = []
            for dot, rec in sorted(pending_for):
                pending_json.append({
                    "dot": [dot[0], dot[1]],
                    "waiting_for": [list(d) for d in self._missing_deps(rec)],
                })

            body: dict = {"key": key, "pending": pending_json}
            if not maximal:
                body["status"] = "empty"
            elif len(maximal) == 1:
                only = maximal[0]
                body["status"] = "ok"
                body["value"] = only.content
                body["winner"] = {
                    "dot": [only.site, only.count],
                    "context": dict(only.context),
                }
            else:
                body["status"] = "conflict"
                body["candidates"] = [
                    {
                        "dot": [r.site, r.count],
                        "site": r.site,
                        "content": r.content,
                        "context": dict(r.context),
                    }
                    for r in maximal
                ]
            return body

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "stations": sorted(self._stations),
                "effective": [
                    {
                        "key": r.key,
                        "site": r.site,
                        "count": r.count,
                        "content": r.content,
                        "context": dict(r.context),
                    }
                    for r in sorted(self._effective.values(),
                                    key=lambda x: (x.site, x.count))
                ],
                "pending": [
                    {
                        "key": r.key,
                        "site": r.site,
                        "count": r.count,
                        "content": r.content,
                        "context": dict(r.context),
                    }
                    for r in sorted(self._pending.values(),
                                    key=lambda x: (x.site, x.count))
                ],
            }

    def _save(self) -> None:
        if not self._data_dir:
            return
        payload = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=".snapshot.", dir=self._data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(self._data_dir, SNAPSHOT_NAME))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load(self) -> None:
        path = os.path.join(self._data_dir, SNAPSHOT_NAME)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") != SNAPSHOT_VERSION:
            raise RuntimeError(f"unsupported snapshot version: {data.get('version')!r}")
        for section in ("effective", "pending"):
            for item in data.get(section, []):
                rec = Record(
                    key=item["key"],
                    site=item["site"],
                    count=item["count"],
                    content=item["content"],
                    context={k: int(v) for k, v in item["context"].items()},
                )
                if rec.site not in self._stations:
                    raise RuntimeError(
                        f"snapshot references unknown station {rec.site}"
                    )
                if section == "effective":
                    self._effective[rec.dot] = rec
                    self._key_dots.setdefault(rec.key, set()).add(rec.dot)
                else:
                    self._pending[rec.dot] = rec
        # Restore the pending/effective invariant: sweep anything whose
        # dependencies are present in the loaded projection.
        progressed = True
        while progressed:
            progressed = False
            for dot, rec in list(self._pending.items()):
                if not self._missing_deps(rec):
                    del self._pending[dot]
                    self._effective[dot] = rec
                    self._key_dots.setdefault(rec.key, set()).add(dot)
                    progressed = True
