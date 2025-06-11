from __future__ import annotations

from typing import Any, Dict, List, Optional


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
        path_specs: List[Dict[str, Any]] = [
            {
                "path": "/platform/chassis",
                "datatype": "state",
                "fields": [
                    "type",
                    "serial-number",
                    "part-number",
                    "hw-mac-address",
                    "last-booted",
                ],
            },
            {
                "path": "/platform/control[slot=A]",
                "datatype": "state",
                "fields": [
                    "software-version",
                ],
            },
        ]
        result: Dict[str, Any] = {}
        for spec in path_specs:
            resp = self.get(paths=[spec.get("path", "")], datatype=spec["datatype"])
            for path in resp[0]:
                result.update(
                    {k: v for k, v in resp[0][path].items() if k in spec["fields"]}
                )
        if result.get("software-version"):
            result["software-version"] = (
                result["software-version"].split("-")[0].lstrip("v")
            )
        return {"sys_info": [result]}
