"""A rendered table and the wire representation shared by its readers.

Tables are published after construction and treated as immutable. Keeping the
encoding on the table makes it live exactly as long as the cached render, with
no second cache retaining old route tables after eviction.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

_TIMINGS = frozenset({"generated", "render_ms", "oldest_update"})
_PREFIX = b"event: table\ndata: "


@dataclass(frozen=True)
class SerializedTable:
    digest: str
    event: bytes

    @property
    def body(self) -> bytes:
        return self.event[len(_PREFIX):-2]


def _encode(table: Dict[str, Any]) -> SerializedTable:
    # Encode the large, visible portion once. Render timings do not cause an
    # SSE update, but checks, cards and graphs do, even if their rows agree.
    visible = {k: v for k, v in table.items() if k not in _TIMINGS}
    material = json.dumps(visible, default=str, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha1(material).hexdigest()
    timings = {k: v for k, v in table.items() if k in _TIMINGS}
    if timings:
        suffix = json.dumps(timings, default=str, separators=(",", ":")).encode()
        body = material[:-1] + (b"," if visible else b"") + suffix[1:]
    else:
        body = material
    return SerializedTable(digest, _PREFIX + body + b"\n\n")


class Table(dict):
    """A published render, lazily encoded once across concurrent clients."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._encoding_lock = threading.Lock()
        self._encoded: Optional[SerializedTable] = None

    def serialized(self) -> SerializedTable:
        with self._encoding_lock:
            if self._encoded is None:
                self._encoded = _encode(self)
            return self._encoded


def serialize_table(table: Dict[str, Any]) -> SerializedTable:
    """Called by a render worker, never on the ASGI event loop."""
    return table.serialized() if isinstance(table, Table) else _encode(table)


def table_digest(table: Dict[str, Any]) -> str:
    return serialize_table(table).digest
