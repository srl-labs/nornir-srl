"""What a fabric went through, kept on disk across restarts.

The live server watches the fabric and keeps a timeline of what changed, a
baseline, and - since it reads the commit log - every configuration the
nodes committed. In memory all of that ends with the process; here it is one
SQLite file per fabric, in ``~/.local/state/fcli/history/``, beside the
snapshots, the cabling and the acknowledgements.

SQLite rather than a file per kind of thing: the questions asked of a history
are ranges - what changed on leaf3 between two and three o'clock, the commits
of the last week - and an index answers those without reading the rest.
It comes with Python, needs no server, and one file is easy to copy, keep
or delete.

The file holds:

* **changes** - every :class:`~nornir_srl.changes.Change` the timeline
  recorded, pruned by age (:meth:`HistoryStore.prune`);
* **readings** - fabric readings kept whole: named baselines, and the last
  reading of the running server, which the next start compares against to
  report what happened while nothing was watching. A reading is the gNMI
  data it was made of, replayed through the report getters to read it back;
  see :mod:`nornir_srl.server.readings`;
* **configs** - each node's running configuration as it stood after each
  commit, redacted (:mod:`nornir_srl.configs`), stored once per distinct
  content;
* **meta** - small values: the active baseline, the watched prefixes, when
  the server was last seen running, the salt the secrets are digested with.

Configurations hold secrets even redacted - their shape, users, addresses -
so the directory is created readable by its owner only.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Bumped when the layout changes; an older file is migrated, a newer one is
#: refused rather than misread.
SCHEMA_VERSION = 1

#: Changes older than this are pruned, unless the server is told otherwise.
DEFAULT_RETENTION_DAYS = 30

#: The name of the reading the running server keeps as the last it took.
LAST_READING = "__last__"

#: Named readings kept besides the last one, the oldest dropped first.
MAX_BASELINES = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    node TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS changes_at ON changes (at);
CREATE INDEX IF NOT EXISTS changes_node_at ON changes (node, at);
CREATE TABLE IF NOT EXISTS readings (
    name TEXT PRIMARY KEY,
    at REAL NOT NULL,
    saved REAL NOT NULL,
    nodes INTEGER NOT NULL,
    findings INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs (
    digest TEXT PRIMARY KEY,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS configs (
    node TEXT NOT NULL,
    commit_id INTEGER NOT NULL,
    at REAL NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL REFERENCES blobs (digest),
    PRIMARY KEY (node, commit_id)
);
CREATE INDEX IF NOT EXISTS configs_node_at ON configs (node, at);
"""


def default_directory() -> Path:
    """Where the history files live unless the server was told otherwise."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(state) / "fcli" / "history"


@dataclass(frozen=True)
class SavedReading:
    """A reading kept on disk, without its payload."""

    name: str
    at: float
    saved: float
    nodes: int
    findings: int
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "at": self.at,
            "saved": self.saved,
            "nodes": self.nodes,
            "findings": self.findings,
            "note": self.note,
        }


@dataclass(frozen=True)
class ConfigVersion:
    """One node's configuration after one commit, without the configuration."""

    node: str
    commit_id: int
    at: float
    username: str
    comment: str
    digest: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "commit": self.commit_id,
            "at": self.at,
            "username": self.username,
            "comment": self.comment,
            "digest": self.digest,
        }


class HistoryError(RuntimeError):
    """The history file cannot be used: newer than this fcli, or not SQLite."""


class HistoryStore:
    """One fabric's history, as a SQLite file.

    Safe to share between threads: every call takes the store's lock, and
    holds the connection only for as long as it runs. Writes are small and
    infrequent - a reading every few seconds at most - so one connection is
    plenty.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:  # pragma: no cover - a directory someone else owns
            pass
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover
            pass
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except sqlite3.DatabaseError as exc:
            self._db.close()
            raise HistoryError(f"{self.path} is not a history file fcli can read: {exc}") from exc

    @classmethod
    def for_fabric(cls, name: str, directory: Optional[Path] = None) -> "HistoryStore":
        """The history of the fabric called *name*, in *directory* or the default one."""
        from .server.snapshots import _slug  # noqa: PLC0415 - one way to name a file

        return cls((directory or default_directory()) / f"{_slug(name or 'fabric')}.sqlite")

    def _migrate(self) -> None:
        with self._lock:
            # executescript commits on its own, so it cannot run inside a
            # transaction; every statement in it is idempotent.
            self._db.executescript(_SCHEMA)
        with self._transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            version = int(row["value"]) if row else 0
            if version > SCHEMA_VERSION:
                raise HistoryError(
                    f"{self.path} was written by a newer fcli (schema {version}, this one reads {SCHEMA_VERSION})"
                )
            if version < SCHEMA_VERSION:
                db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ #
    # meta
    # ------------------------------------------------------------------ #

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Optional[str]) -> None:
        with self._transaction() as db:
            if value is None:
                db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def salt(self) -> str:
        """What the secrets in this fabric's configurations are digested with."""
        with self._lock:
            value = self.get_meta("salt")
            if value is None:
                value = secrets.token_hex(16)
                self.set_meta("salt", value)
        return value

    def watched(self) -> List[str]:
        raw = self.get_meta("watched")
        try:
            return [str(p) for p in json.loads(raw)] if raw else []
        except ValueError:
            return []

    def set_watched(self, prefixes: Iterable[str]) -> None:
        self.set_meta("watched", json.dumps(sorted(set(prefixes))))

    # ------------------------------------------------------------------ #
    # changes
    # ------------------------------------------------------------------ #

    def add_changes(self, changes: Iterable[Any]) -> int:
        """Keep *changes*; returns how many."""
        rows = [
            (c.at, c.node, c.kind, c.subject, c.before, c.after, c.severity, c.detail or "")
            for c in changes
        ]
        if not rows:
            return 0
        with self._transaction() as db:
            db.executemany(
                "INSERT INTO changes (at, node, kind, subject, before, after, severity, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def changes(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
        nodes: Optional[Sequence[str]] = None,
        kinds: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Any]:
        """The changes kept in a time range, newest first.

        *nodes* and *kinds* narrow it down; a ``server`` change - fcli itself
        stopping or starting - is about every node, and is kept whatever
        *nodes* says.
        """
        from .changes import Change  # noqa: PLC0415 - changes imports the checks

        query = "SELECT * FROM changes WHERE 1 = 1"
        args: List[Any] = []
        if since is not None:
            query += " AND at >= ?"
            args.append(since)
        if until is not None:
            query += " AND at <= ?"
            args.append(until)
        if nodes is not None:
            wanted = list(nodes)
            query += f" AND (kind = 'server' OR node IN ({','.join('?' * len(wanted)) or 'NULL'}))"
            args.extend(wanted)
        if kinds:
            query += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        query += " ORDER BY at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [
            Change(
                at=row["at"],
                node=row["node"],
                kind=row["kind"],
                subject=row["subject"],
                before=row["before"],
                after=row["after"],
                severity=row["severity"],
                detail=row["detail"],
            )
            for row in rows
        ]

    def change_count(self) -> Tuple[int, Optional[float]]:
        """How many changes are kept, and when the oldest is from."""
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n, MIN(at) AS oldest FROM changes").fetchone()
        return int(row["n"]), row["oldest"]

    def prune(self, retention_days: float = DEFAULT_RETENTION_DAYS, now: Optional[float] = None) -> int:
        """Drop changes older than *retention_days*; returns how many went.

        Readings and configurations are not aged out: a baseline is kept until
        someone deletes it, and a configuration is what the next one is
        compared against.
        """
        if retention_days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - retention_days * 86400
        with self._transaction() as db:
            cursor = db.execute("DELETE FROM changes WHERE at < ?", (cutoff,))
        return cursor.rowcount or 0

    # ------------------------------------------------------------------ #
    # readings
    # ------------------------------------------------------------------ #

    def save_reading(
        self,
        name: str,
        at: float,
        payload: Dict[str, Any],
        *,
        nodes: int = 0,
        findings: int = 0,
        note: str = "",
    ) -> SavedReading:
        """Keep a reading under *name*, replacing one of the same name."""
        blob = gzip.compress(json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8"))
        saved = time.time()
        with self._transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO readings (name, at, saved, nodes, findings, note, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, at, saved, nodes, findings, note, blob),
            )
            if name != LAST_READING:
                stale = db.execute(
                    "SELECT name FROM readings WHERE name != ? ORDER BY saved DESC LIMIT -1 OFFSET ?",
                    (LAST_READING, MAX_BASELINES),
                ).fetchall()
                for row in stale:
                    db.execute("DELETE FROM readings WHERE name = ?", (row["name"],))
        return SavedReading(name=name, at=at, saved=saved, nodes=nodes, findings=findings, note=note)

    def readings(self, include_last: bool = False) -> List[SavedReading]:
        """The readings kept, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT name, at, saved, nodes, findings, note FROM readings ORDER BY at DESC"
            ).fetchall()
        return [
            SavedReading(
                name=row["name"],
                at=row["at"],
                saved=row["saved"],
                nodes=row["nodes"],
                findings=row["findings"],
                note=row["note"],
            )
            for row in rows
            if include_last or row["name"] != LAST_READING
        ]

    def load_reading(self, name: str) -> Optional[Tuple[SavedReading, Dict[str, Any]]]:
        """The reading kept under *name*, with its payload; ``None`` if there is none."""
        with self._lock:
            row = self._db.execute("SELECT * FROM readings WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(gzip.decompress(row["payload"]).decode("utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("reading '%s' in %s cannot be read: %s", name, self.path, exc)
            return None
        meta = SavedReading(
            name=row["name"],
            at=row["at"],
            saved=row["saved"],
            nodes=row["nodes"],
            findings=row["findings"],
            note=row["note"],
        )
        return meta, payload

    def delete_reading(self, name: str) -> bool:
        with self._transaction() as db:
            cursor = db.execute("DELETE FROM readings WHERE name = ?", (name,))
            if cursor.rowcount and self.get_meta("baseline") == name:
                db.execute("DELETE FROM meta WHERE key = 'baseline'")
        return bool(cursor.rowcount)

    # ------------------------------------------------------------------ #
    # configurations
    # ------------------------------------------------------------------ #

    def save_config(
        self,
        node: str,
        commit_id: int,
        tree: Dict[str, Any],
        *,
        at: Optional[float] = None,
        username: str = "",
        comment: str = "",
    ) -> ConfigVersion:
        """Keep *node*'s configuration as it stood after *commit_id*.

        *tree* is already normalized and redacted (:func:`nornir_srl.configs.normalize`).
        The same content is stored once, however many commits it stood after.
        """
        from .configs import canonical, digest  # noqa: PLC0415

        key = digest(tree)
        at = time.time() if at is None else at
        blob = gzip.compress(canonical(tree).encode("utf-8"))
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO blobs (digest, payload) VALUES (?, ?)", (key, blob))
            db.execute(
                "INSERT OR REPLACE INTO configs (node, commit_id, at, username, comment, digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node, int(commit_id), at, username, comment, key),
            )
        return ConfigVersion(node=node, commit_id=int(commit_id), at=at, username=username, comment=comment, digest=key)

    def config_versions(self, node: Optional[str] = None, limit: Optional[int] = None) -> List[ConfigVersion]:
        """The configurations kept, newest commit first, for one node or all."""
        query = "SELECT node, commit_id, at, username, comment, digest FROM configs"
        args: List[Any] = []
        if node is not None:
            query += " WHERE node = ?"
            args.append(node)
        query += " ORDER BY node, commit_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [
            ConfigVersion(
                node=row["node"],
                commit_id=row["commit_id"],
                at=row["at"],
                username=row["username"],
                comment=row["comment"],
                digest=row["digest"],
            )
            for row in rows
        ]

    def latest_config(self, node: str, before: Optional[int] = None) -> Optional[ConfigVersion]:
        """*node*'s newest configuration kept, or the newest before commit *before*."""
        query = "SELECT node, commit_id, at, username, comment, digest FROM configs WHERE node = ?"
        args: List[Any] = [node]
        if before is not None:
            query += " AND commit_id < ?"
            args.append(int(before))
        query += " ORDER BY commit_id DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(query, args).fetchone()
        if row is None:
            return None
        return ConfigVersion(
            node=row["node"],
            commit_id=row["commit_id"],
            at=row["at"],
            username=row["username"],
            comment=row["comment"],
            digest=row["digest"],
        )

    def config(self, node: str, commit_id: Optional[int] = None) -> Optional[Tuple[ConfigVersion, Dict[str, Any]]]:
        """*node*'s configuration after *commit_id* (the newest kept, if ``None``)."""
        version = (
            self.latest_config(node)
            if commit_id is None
            else next((v for v in self.config_versions(node) if v.commit_id == int(commit_id)), None)
        )
        if version is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT payload FROM blobs WHERE digest = ?", (version.digest,)).fetchone()
        if row is None:
            return None
        return version, json.loads(gzip.decompress(row["payload"]).decode("utf-8"))


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "LAST_READING",
    "ConfigVersion",
    "HistoryError",
    "HistoryStore",
    "SavedReading",
    "default_directory",
]
