from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..records import ConfigCommit, SystemInfo
from .helpers import as_list, first_payload


def _container(resp: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The leaves of the one container a Get for it answers with.

    The payload is keyed by the path as the device echoes it - ``platform/chassis``
    or ``platform/control[slot=A]`` - so the value is taken whatever the key.
    """
    for value in first_payload(resp).values():
        if isinstance(value, dict):
            return value
    return {}


class SystemMixin:
    """Mixin providing system related getters."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_info(self) -> Dict[str, Any]:
        """Return system information such as chassis and software details."""
        chassis = _container(self.get(paths=["/platform/chassis"], datatype="state"))
        control = _container(self.get(paths=["/platform/control[slot=A]"], datatype="state"))
        # The release alone: SR Linux reports ``v26.7.1-554-g78ed635f70a``.
        version = str(control.get("software-version") or "").split("-")[0].lstrip("v")
        info = SystemInfo(
            type=str(chassis.get("type") or ""),
            serial_number=str(chassis.get("serial-number") or ""),
            part_number=str(chassis.get("part-number") or ""),
            hw_mac_address=str(chassis.get("hw-mac-address") or ""),
            last_booted=str(chassis.get("last-booted") or ""),
            software_version=version,
        )
        return {"sys_info": [info]}

    def get_commits(self) -> Dict[str, Any]:
        """Return the node's log of commits to its running configuration."""
        payload = first_payload(self.get(paths=["/system/configuration/commit"], datatype="state"))
        entries: List[Any] = []
        for value in payload.values():
            # Keyed by the path as the device echoes it, which is the list
            # itself or the container holding it.
            if isinstance(value, dict) and "id" not in value:
                value = value.get("commit")
            entries.extend(as_list(value))
        commits = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                commit_id = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            commits.append(
                ConfigCommit(
                    id=commit_id,
                    status=str(entry.get("status") or ""),
                    username=str(entry.get("username") or ""),
                    comment=str(entry.get("comment") or ""),
                    type=str(entry.get("type") or ""),
                    session=str(entry.get("name") or ""),
                    started=str(entry.get("started") or ""),
                    ended=str(entry.get("ended") or ""),
                )
            )
        commits.sort(key=lambda c: c.id)
        return {"config_commits": commits}
