"""Small revision counters for the schema paths a report reads.

List keys are deliberately collapsed: changing one route may invalidate a
query for another route in that same list, but a million routes still need
only a handful of counters. Sibling branches (BGP and interface statistics,
for example) remain independent. Ancestor replacements and deletes affect
every descendant read.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Tuple

from .tree import parse_path

Shape = Tuple[str, ...]


@lru_cache(maxsize=2048)
def shape(path: str) -> Shape:
    return tuple(name for name, _keys in parse_path(path))


class PathVersions:
    def __init__(self) -> None:
        self._sequence = 0
        self._exact: Dict[Shape, int] = {}
        self._below: Dict[Shape, int] = {}

    def touch(self, path: str) -> None:
        parts = shape(path)
        self._sequence += 1
        self._exact[parts] = self._sequence
        for end in range(len(parts) + 1):
            self._below[parts[:end]] = self._sequence

    def version(self, parts: Shape) -> int:
        return max(
            self._below.get(parts, 0),
            max((self._exact.get(parts[:end], 0) for end in range(len(parts) + 1)), default=0),
        )


@dataclass
class ReadDependencies:
    versions: Dict[Shape, int] = field(default_factory=dict)
    expires: float = float("inf")
