"""Interface statistics mixin – computes in/out bps from two consecutive gNMI samples."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class InterfaceStatsMixin:
    """Mixin providing interface traffic-rate statistics."""

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        """Placeholder method implemented in :class:`SrLinux`."""
        raise NotImplementedError

    def get_ifstats(self, interface: str = "*", interval: int = 5) -> Dict[str, Any]:
        """Return per-interface in/out bps computed from two samples *interval* seconds apart.

        Args:
            interface: Interface name filter (default ``*`` = all interfaces).
            interval: Seconds between the two gNMI samples (default 5).
        """
        path = f"/interface[name={interface}]/statistics"

        def _sample() -> tuple:
            resp = self.get(paths=[path], datatype="state")
            ts = time.monotonic()
            return resp, ts

        resp1, t1 = _sample()
        time.sleep(interval)
        resp2, t2 = _sample()

        dt = t2 - t1

        # Build lookup: interface-name -> {in-octets, out-octets} for each sample
        def _parse(resp: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
            result: Dict[str, Dict[str, int]] = {}
            for itf in resp[0].get("interface", []):
                name = itf.get("name", "")
                stats = itf.get("statistics", {})
                result[name] = {
                    "in-octets": int(stats.get("in-octets", 0)),
                    "out-octets": int(stats.get("out-octets", 0)),
                    "in-errors": int(stats.get("in-error-packets", 0)),
                    "out-errors": int(stats.get("out-error-packets", 0)),
                    "in-discards": int(stats.get("in-discarded-packets", 0)),
                    "out-discards": int(stats.get("out-discarded-packets", 0)),
                }
            return result

        s1 = _parse(resp1)
        s2 = _parse(resp2)

        rows: List[Dict[str, Any]] = []
        for name in sorted(s2.keys()):
            if name not in s1:
                continue
            d_in = s2[name]["in-octets"] - s1[name]["in-octets"]
            d_out = s2[name]["out-octets"] - s1[name]["out-octets"]
            in_bps = round(d_in * 8 / dt)
            out_bps = round(d_out * 8 / dt)
            in_err = s2[name]["in-errors"] - s1[name]["in-errors"]
            out_err = s2[name]["out-errors"] - s1[name]["out-errors"]
            in_disc = s2[name]["in-discards"] - s1[name]["in-discards"]
            out_disc = s2[name]["out-discards"] - s1[name]["out-discards"]
            if in_bps or out_bps:
                rows.append(
                    {
                        "interface": name,
                        "in-Kbps": round(in_bps / 1000, 1),
                        "out-Kbps": round(out_bps / 1000, 1),
                        "in-err": in_err,
                        "out-err": out_err,
                        "in-disc": in_disc,
                        "out-disc": out_disc,
                    }
                )

        return {"ifstats": rows}
