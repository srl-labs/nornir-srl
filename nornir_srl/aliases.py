"""Matching the name a node advertises against the name the inventory uses.

LLDP identifies a neighbour by the system-name it advertises, which is rarely
the name the inventory knows it by: containerlab prefixes the inventory with
the lab name while the node keeps its short hostname, and a real fabric hands
out FQDNs. Anything correlating one node's view of the fabric with another's
has to bridge that gap, and has to bridge it the same way, or the topology
drawing and the checks disagree about which links exist.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set


def tail(name: str) -> str:
    """The last dash-separated segment: ``clab-dc1-leaf1`` is ``leaf1``."""
    return name.rsplit("-", 1)[-1] or name


def alias_index(nodes: Iterable[Sequence[str]]) -> Dict[str, str]:
    """Map every name a node may be known by onto its inventory name.

    *nodes* is one sequence of names per node, the first of which is the
    inventory name. An alias two nodes could both answer to is dropped rather
    than guessed at.
    """
    direct: Dict[str, Set[str]] = {}
    tails: Dict[str, Set[str]] = {}
    for names in nodes:
        if not names:
            continue
        owner = names[0]
        for alias in names:
            if not alias:
                continue
            text = alias.strip().lower()
            short = text.split(".")[0]
            direct.setdefault(text, set()).add(owner)
            direct.setdefault(short, set()).add(owner)
            tails.setdefault(tail(short), set()).add(owner)
    index = {
        alias: next(iter(owners)) for alias, owners in direct.items() if len(owners) == 1
    }
    for alias, owners in tails.items():
        if alias not in index and len(owners) == 1:
            index[alias] = next(iter(owners))
    return index


def resolve(advertised: str, index: Mapping[str, str]) -> Optional[str]:
    """The inventory name of an advertised system-name, if we have that node."""
    text = advertised.strip().lower()
    short = text.split(".")[0]
    return index.get(text) or index.get(short) or index.get(tail(short))


def inventory_index(hosts: Mapping[str, Any]) -> Dict[str, str]:
    """:func:`alias_index` of a Nornir inventory's hosts: every name each answers to."""
    return alias_index([(name, getattr(host, "hostname", "") or "") for name, host in hosts.items()])
