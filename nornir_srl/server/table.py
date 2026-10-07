"""A rendered table and the wire representation shared by its readers.

Tables are published after construction and treated as immutable. Keeping the
encoding on the table makes it live exactly as long as the cached render, with
no second cache retaining old route tables after eviction.

A report table is built from one :class:`RowPart` per node. A node whose
state did not change keeps its part from the previous render, encoding and
all, so re-rendering a fabric after one node changed re-encodes that node's
rows alone, and a stream can send the browser that node's rows alone: a
patch rather than the whole table.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

_TIMINGS = frozenset({"generated", "render_ms", "oldest_update"})
_PREFIX = b"event: table\ndata: "
_PATCH = b"event: patch\ndata: "


def _dumps(value: Any) -> bytes:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()


class RowPart:
    """One node's rows, encoded once for every table that reuses them."""

    __slots__ = ("key", "rows", "_lock", "_encoded")

    def __init__(self, key: str, rows: List[Dict[str, Any]]) -> None:
        self.key = key
        self.rows = rows
        self._lock = threading.Lock()
        self._encoded: Optional[Tuple[bytes, str]] = None

    def encoded(self) -> Tuple[bytes, str]:
        """The rows as JSON array items (no brackets), and their digest."""
        with self._lock:
            if self._encoded is None:
                items = _dumps(self.rows)[1:-1]
                self._encoded = (items, hashlib.sha1(items).hexdigest())
            return self._encoded


@dataclass(frozen=True)
class SerializedTable:
    digest: str
    event: bytes
    #: For a table built from parts: what has to match for a patch to apply
    #: (report, title, columns), everything but the rows and their timings,
    #: and each part as (key, digest, encoded rows).
    shape: Optional[str] = None
    head: Optional[bytes] = None
    parts: Optional[Tuple[Tuple[str, str, bytes], ...]] = None

    @property
    def body(self) -> bytes:
        return self.event[len(_PREFIX):-2]

    def patch_from(self, previous: "SerializedTable") -> Optional[bytes]:
        """An SSE ``patch`` event turning *previous* into this table, if one applies.

        Only the parts whose digest changed are sent, with the order of every
        part, so the browser rebuilds the rows from what it already has.
        """
        if self.parts is None or previous.parts is None or self.shape != previous.shape:
            return None
        before = {key: digest for key, digest, _ in previous.parts}
        changed = [(key, rows) for key, digest, rows in self.parts if before.get(key) != digest]
        order = _dumps([key for key, _, _ in self.parts])
        body = b",".join(_dumps(key) + b":[" + rows + b"]" for key, rows in changed)
        return _PATCH + b'{"head":' + self.head + b',"order":' + order + b',"parts":{' + body + b"}}\n\n"


def _timings(table: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in table.items() if k in _TIMINGS}


def _encode(table: Dict[str, Any], parts: Optional[Sequence[RowPart]] = None) -> SerializedTable:
    # Encode the large, visible portion once. Render timings do not cause an
    # SSE update, but checks, cards and graphs do, even if their rows agree.
    timings = _timings(table)
    if parts is None:
        visible = {k: v for k, v in table.items() if k not in _TIMINGS}
        material = _dumps(visible)
        digest = hashlib.sha1(material).hexdigest()
        if timings:
            suffix = json.dumps(timings, default=str, separators=(",", ":")).encode()
            body = material[:-1] + (b"," if visible else b"") + suffix[1:]
        else:
            body = material
        return SerializedTable(digest, _PREFIX + body + b"\n\n")
    # Rows come from the parts: the browser keeps them per part, by key, to
    # apply later patches to.
    meta = {k: v for k, v in table.items() if k not in _TIMINGS and k != "rows"}
    meta["row_parts"] = [[p.key, len(p.rows)] for p in parts]
    encoded = [p.encoded() for p in parts]
    meta_bytes = _dumps(meta)
    digest = hashlib.sha1(meta_bytes + b"".join(d.encode() for _, d in encoded)).hexdigest()
    rows = b"[" + b",".join(items for items, _ in encoded if items) + b"]"
    tail = (b"," + json.dumps(timings, default=str, separators=(",", ":")).encode()[1:-1]) if timings else b""
    body = meta_bytes[:-1] + b',"rows":' + rows + tail + b"}"
    head = {k: v for k, v in table.items() if k != "rows"}
    shape = _dumps([table.get("report"), table.get("title"), table.get("columns")]).decode()
    return SerializedTable(
        digest,
        _PREFIX + body + b"\n\n",
        shape=shape,
        head=_dumps(head),
        parts=tuple((p.key, d, items) for p, (items, d) in zip(parts, encoded)),
    )


class Table(dict):
    """A published render, lazily encoded once across concurrent clients."""

    def __init__(self, *args: Any, parts: Optional[Sequence[RowPart]] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._encoding_lock = threading.Lock()
        self._encoded: Optional[SerializedTable] = None
        #: The rows by node, when the table was built from them.
        self.parts: Optional[Tuple[RowPart, ...]] = tuple(parts) if parts is not None else None

    def serialized(self) -> SerializedTable:
        with self._encoding_lock:
            if self._encoded is None:
                self._encoded = _encode(self, self.parts)
            return self._encoded


def serialize_table(table: Dict[str, Any]) -> SerializedTable:
    """Called by a render worker, never on the ASGI event loop."""
    return table.serialized() if isinstance(table, Table) else _encode(table)


def table_digest(table: Dict[str, Any]) -> str:
    return serialize_table(table).digest
