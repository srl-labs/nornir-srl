"""A node's running configuration, as something to keep and compare.

The configuration is read the way every report reads state: a gNMI ``Get``,
here of ``/`` with datatype ``config``, answering one JSON tree. Three things
happen to it before it is kept or shown:

* :func:`normalize` takes out what is not configuration - module prefixes on
  names and identity values, the annotations tools leave behind - and replaces
  every secret with a digest of it (:func:`redact`). A password that changed
  still reads as changed; what it was or became never leaves this module.
* :func:`flatten` renders the tree as the ``set / ...`` lines SR Linux prints
  with ``info flat``, one line per leaf, which is what an engineer reads a
  configuration change in.
* :func:`diff_lines` compares two of those renderings line by line.

JSON carries a YANG list as an array of entries without saying which of
their leaves are the keys. Without the schema, the keys are worked out from
the entries (:func:`list_keys`): the well-known key names SR Linux uses, in
order of how often they are the key, extended until they tell the entries
apart. Both sides of a comparison are read together, so a list is keyed the
same way on both.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

#: The leaves whose value is a secret: a password or its hash, a key, a
#: pre-shared or authentication key, an SNMP community. Matched on the leaf's
#: own name, so ``ssh-key`` (a public key) and ``keychain`` (a list of them,
#: whose keys are themselves redacted) are not.
SECRET_LEAF = re.compile(
    r"^(?:password|hashed-password|secret|key|private-key|pre-shared-key|psk|"
    r"authentication-key|auth-key|auth-password|priv-password|privacy-password|"
    r"shared-secret|community|token|client-secret|md5-key)$"
    r"|-password$|-secret$|-psk$",
    re.IGNORECASE,
)

#: What a redacted value reads as: the digest tells a changed secret from an
#: unchanged one, and nothing else.
REDACTED = "<redacted {}>"

#: Keys that are annotations rather than configuration.
_ANNOTATION = re.compile(r"^_annotate")

#: A module prefix on a node name: ``srl_nokia-system:system``. A YANG
#: identifier cannot hold a colon, so in a name any identifier before one is
#: the module it comes from, whoever wrote that module.
_NAME_MODULE = re.compile(r"^[A-Za-z_][\w.-]*:(?=[A-Za-z_])")

#: A module prefix on an identity value: ``srl_nokia-aaa-types:local``. A
#: value can be any string - ``site:west``, a MAC, a password - so only the
#: modules SR Linux answers with are taken for a prefix.
_VALUE_MODULE = re.compile(r"^(?:srl_nokia|openconfig|ietf|iana)-[\w.-]*:(?=[A-Za-z_])")

#: The names SR Linux keys its lists by, most common first. A list is keyed by
#: the first of these its entries all carry, then the next, until the entries
#: are told apart.
KEY_NAMES: Tuple[str, ...] = (
    "name",
    "id",
    "index",
    "sequence-id",
    "interface-name",
    "peer-address",
    "group-name",
    "ip-prefix",
    "ipv4-prefix",
    "ipv6-prefix",
    "prefix",
    "address",
    "area-id",
    "level-number",
    "system-id",
    "router-id",
    "vlan-id",
    "esi",
    "vni",
    "target",
    "type",
    "afi-safi-name",
    "key-id",
    "entry-id",
    "label",
)

#: Lists whose key is known to need more than one leaf, keyed by the list's
#: own name: the same ACL can be defined once for IPv4 and once for IPv6.
KNOWN_KEYS: Mapping[str, Tuple[str, ...]] = {
    "acl-filter": ("name", "type"),
}


def strip_module(name: str) -> str:
    """A node name without its module: ``srl_nokia-system:system`` is ``system``."""
    return _NAME_MODULE.sub("", name)


def strip_value_module(value: str) -> str:
    """An identity value without its module: ``srl_nokia-aaa-types:local`` is ``local``.

    Anything else is left as it is: ``site:west`` and ``aa:bb:cc:dd:ee:ff``
    are values, not identities.
    """
    return _VALUE_MODULE.sub("", value)


def _digest(value: Any, salt: str) -> str:
    raw = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256((salt + raw).encode("utf-8")).hexdigest()[:8]


#: A list key in a path: ``[name=admin]``, ``[prefix=10.0.0.0/8]``.
_PREDICATE = re.compile(r"\[[^\]]*\]")


def leaf_name(key: Any) -> str:
    """The leaf a key or path names: ``srl_nokia-aaa:user[name=x]/password`` is ``password``.

    A Get below the root answers keyed by the path it was asked for, with
    module prefixes and list keys, rather than by the leaf's own name.
    """
    parts = [p for p in _PREDICATE.sub("", str(key)).split("/") if p]
    return strip_module(parts[-1]) if parts else ""


def redact(tree: Any, salt: str = "", name: str = "") -> Any:
    """*tree* with every secret leaf's value replaced by a digest of it.

    *salt* is mixed into the digest, so the digests of one store say nothing
    about the secrets of another, and a weak password cannot be looked up in
    a table of digested ones. *name* is the leaf *tree* is the value of; a
    key that names no leaf, such as the ``/`` a Get of one leaf answers
    with, keeps it.
    """
    if isinstance(tree, dict):
        return {key: redact(value, salt, leaf_name(key) or name) for key, value in tree.items()}
    if isinstance(tree, list):
        # An SNMP community is one leaf; a leaf-list of them is BGP's.
        if (
            name
            and SECRET_LEAF.search(name)
            and name.lower() != "community"
            and all(not isinstance(v, (dict, list)) for v in tree)
        ):
            return [REDACTED.format(_digest(v, salt)) for v in tree]
        return [redact(value, salt, name if _unnamed(value) else "") for value in tree]
    if name and SECRET_LEAF.search(name) and tree is not None:
        return REDACTED.format(_digest(tree, salt))
    if isinstance(tree, str) and "\n" in tree:
        return redact_text(tree, salt)
    return tree


def _unnamed(value: Any) -> bool:
    """A payload keyed by nothing but ``/``, which is about the leaf asked for."""
    return isinstance(value, dict) and bool(value) and all(not leaf_name(k) for k in value)


#: A secret leaf as CLI text prints it, value last on its line:
#: ``password $y$...`` or ``authentication-key "$aes1$..."``.
_SECRET_TEXT = re.compile(
    r"(?<![\w-])("
    r"(?:password|hashed-password|secret|key|private-key|pre-shared-key|psk|"
    r"authentication-key|auth-key|auth-password|priv-password|privacy-password|"
    r"shared-secret|community|token|client-secret|md5-key|"
    r"[\w-]+-password|[\w-]+-secret|[\w-]+-psk)"
    r"[ \t]+)"
    r"(\"(?:[^\"\\\n]|\\.)*\"|[^\s\"]+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def redact_text(text: str, salt: str = "") -> str:
    """CLI output with every secret leaf's value replaced by a digest of it.

    ``info`` prints one leaf per line with its value last, so only a value
    that ends its line is taken: ``key`` in the middle of a sentence is not.
    """
    return _SECRET_TEXT.sub(
        lambda m: m.group(1) + REDACTED.format(_digest(m.group(2).strip('"'), salt)), text
    )


def _clean(tree: Any) -> Any:
    """*tree* without module prefixes or annotations."""
    if isinstance(tree, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in tree.items():
            if _ANNOTATION.match(str(key)):
                continue
            name = strip_module(str(key))
            # A path-shaped key - ``system/aaa`` from a Get below the root -
            # is the containers it names, nested.
            parts = [strip_module(p) for p in name.split("/") if p]
            target = cleaned
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            leaf = parts[-1] if parts else name
            value = _clean(value)
            if isinstance(target.get(leaf), dict) and isinstance(value, dict):
                target[leaf].update(value)
            else:
                target[leaf] = value
        return cleaned
    if isinstance(tree, list):
        return [_clean(value) for value in tree]
    if isinstance(tree, str):
        return strip_value_module(tree)
    return tree


def normalize(response: Any, salt: str = "") -> Dict[str, Any]:
    """The configuration tree a ``Get`` of ``/`` answered, clean and redacted.

    Takes the response as the connection returns it - a list of payloads,
    each keyed by the path it answers - or a tree already unwrapped.

    Secrets are redacted first, from the values exactly as the node sent
    them: cleaning a value before it is digested could make two different
    secrets read as the same one.
    """
    tree: Dict[str, Any] = {}
    payloads = redact(response if isinstance(response, list) else [response], salt)
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            # The root answers as ``/`` (or nothing at all); anything below it
            # as the path it is at.
            if key in ("/", "", None) and isinstance(value, dict):
                tree = _merge(tree, _clean(value))
            else:
                tree = _merge(tree, _clean({key: value}))
    return tree


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in extra.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def canonical(tree: Any) -> str:
    """*tree* as the one JSON text it is kept and compared as."""
    return json.dumps(tree, sort_keys=True, separators=(",", ":"), default=str)


def digest(tree: Any) -> str:
    """What tells two configurations apart without comparing them."""
    return hashlib.sha256(canonical(tree).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# set-style lines
# --------------------------------------------------------------------------- #


def _scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _is_list(value: Any) -> bool:
    """A YANG list: an array of entries, as opposed to a leaf-list of values."""
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


@dataclass
class _ListShape:
    """Every occurrence of one list, across the trees read together."""

    occurrences: List[List[Dict[str, Any]]] = field(default_factory=list)


def _collect(tree: Any, path: Tuple[str, ...], shapes: Dict[Tuple[str, ...], _ListShape]) -> None:
    if isinstance(tree, dict):
        for key, value in tree.items():
            if _is_list(value):
                signature = path + (key,)
                shapes.setdefault(signature, _ListShape()).occurrences.append(value)
                for entry in value:
                    _collect(entry, signature, shapes)
            elif isinstance(value, dict):
                _collect(value, path + (key,), shapes)


def _unique(occurrences: Sequence[Sequence[Dict[str, Any]]], keys: Sequence[str]) -> bool:
    for entries in occurrences:
        seen = set()
        for entry in entries:
            value = tuple(str(entry.get(k)) for k in keys)
            if value in seen:
                return False
            seen.add(value)
    return True


def list_keys(trees: Iterable[Any]) -> Dict[Tuple[str, ...], Tuple[str, ...]]:
    """The leaves that key each list found in *trees*, by the list's path.

    A list's path is the names of the containers and lists down to it,
    without keys: ``("interface", "subinterface")``.
    """
    shapes: Dict[Tuple[str, ...], _ListShape] = {}
    for tree in trees:
        _collect(tree, (), shapes)
    keys: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    for signature, shape in shapes.items():
        entries = [entry for occurrence in shape.occurrences for entry in occurrence]
        # A leaf every entry carries as a value is a candidate key.
        common = [
            name
            for name in dict.fromkeys(name for entry in entries for name in entry)
            if all(name in entry and _scalar(entry[name]) for entry in entries)
        ]
        known = KNOWN_KEYS.get(signature[-1])
        if known and all(k in common for k in known):
            keys[signature] = known
            continue
        ordered = [k for k in KEY_NAMES if k in common] + sorted(k for k in common if k not in KEY_NAMES)
        chosen: List[str] = []
        for name in ordered:
            chosen.append(name)
            if _unique(shape.occurrences, chosen):
                break
        keys[signature] = tuple(chosen)
    return keys


_BARE = re.compile(r"^[^\s\"';{}\[\]]+$")


def _token(value: Any) -> str:
    """A value as SR Linux prints it in a flat line: quoted when it has to be."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text and _BARE.match(text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def flatten(tree: Any, keys: Optional[Mapping[Tuple[str, ...], Tuple[str, ...]]] = None) -> List[str]:
    """*tree* as ``set / ...`` lines, one per leaf, in the order SR Linux prints them.

    *keys* says how each list is keyed, as :func:`list_keys` works it out;
    without it, the tree is keyed on its own.
    """
    keys = list_keys([tree]) if keys is None else keys
    lines = list(_lines(tree, (), (), keys))
    return lines


def _lines(
    tree: Any,
    words: Tuple[str, ...],
    signature: Tuple[str, ...],
    keys: Mapping[Tuple[str, ...], Tuple[str, ...]],
) -> Iterator[str]:
    if not isinstance(tree, dict):
        return
    if not tree and words:
        # A presence container, or one configured with nothing in it.
        yield "set / " + " ".join(words)
        return
    for key in sorted(tree, key=_natural):
        value = tree[key]
        if _is_list(value):
            list_signature = signature + (key,)
            key_names = keys.get(list_signature) or ()
            for entry in sorted(value, key=lambda e: tuple(_natural(str(e.get(k, ""))) for k in key_names)):
                head: List[str] = [key]
                for n, name in enumerate(key_names):
                    if n:
                        head.append(name)
                    head.append(_token(entry.get(name, "")))
                rest = {k: v for k, v in entry.items() if k not in key_names}
                if not rest:
                    yield "set / " + " ".join(words + tuple(head))
                    continue
                yield from _lines(rest, words + tuple(head), list_signature, keys)
        elif isinstance(value, dict):
            yield from _lines(value, words + (key,), signature + (key,), keys)
        elif isinstance(value, list):
            # A leaf-list, printed whole: ``server-list [ 10.0.0.1 10.0.0.2 ]``.
            values = " ".join([_token(v) for v in value] + ["]"])
            yield "set / " + " ".join(words + (key, "[", values))
        else:
            yield "set / " + " ".join(words + (key, _token(value)))


def _natural(text: str) -> Tuple[Any, ...]:
    """``ethernet-1/10`` after ``ethernet-1/2``."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(text)))


# --------------------------------------------------------------------------- #
# comparing
# --------------------------------------------------------------------------- #

#: A line only the newer configuration has.
ADDED = "+"
#: A line only the older one has.
REMOVED = "-"


@dataclass(frozen=True)
class ConfigDiff:
    """Two configurations of one node, compared line by line."""

    #: (``+`` or ``-``, the line), in configuration order.
    lines: Tuple[Tuple[str, str], ...]

    @property
    def added(self) -> int:
        return sum(1 for sign, _ in self.lines if sign == ADDED)

    @property
    def removed(self) -> int:
        return sum(1 for sign, _ in self.lines if sign == REMOVED)

    @property
    def summary(self) -> str:
        """``+3 -1 lines``, or ``no change`` for two identical configurations."""
        if not self.lines:
            return "no change"
        return f"+{self.added} -{self.removed} lines"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "added": self.added,
            "removed": self.removed,
            "summary": self.summary,
            "lines": [{"op": sign, "line": line} for sign, line in self.lines],
        }

    def text(self) -> str:
        return "\n".join(f"{sign} {line}" for sign, line in self.lines)


def diff_trees(before: Optional[Any], after: Any) -> ConfigDiff:
    """*after* compared with *before*, as lines added and removed.

    Both are keyed together, so a list reads the same on both sides. With no
    *before*, every line of *after* is new.
    """
    trees = [t for t in (before, after) if t is not None]
    keys = list_keys(trees)
    old = flatten(before, keys) if before is not None else []
    new = flatten(after, keys)
    return diff_lines(old, new)


def diff_lines(before: Sequence[str], after: Sequence[str]) -> ConfigDiff:
    """The lines only one side has, in the order the configuration prints them."""
    gone: Set[str] = set(before) - set(after)
    came: Set[str] = set(after) - set(before)
    marked = [(line, REMOVED) for line in gone] + [(line, ADDED) for line in came]
    # Where a leaf changed value, its old line reads just before its new one.
    marked.sort(key=lambda item: (_natural(_leaf_path(item[0])), item[1] != REMOVED, item[0]))
    return ConfigDiff(lines=tuple((sign, line) for line, sign in marked))


def _leaf_path(line: str) -> str:
    """A line without its value: what its old and new versions have in common."""
    head, _, last = line.rpartition(" ")
    if last == "]":
        head = line.split(" [", 1)[0]
    return head or line


__all__ = [
    "ADDED",
    "REMOVED",
    "ConfigDiff",
    "canonical",
    "diff_lines",
    "diff_trees",
    "digest",
    "flatten",
    "list_keys",
    "normalize",
    "leaf_name",
    "redact",
    "redact_text",
    "strip_module",
    "strip_value_module",
]
