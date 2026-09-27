"""Re-derive the golden tables of the recordings from their recorded payloads.

A recording holds both the gNMI exchange of a report and the table it produced
live. Only the exchange needs a lab: the table is what the flattener makes of
it, so when the flattener changes on purpose the goldens can be brought up to
date offline, without re-recording anything.

    python -m tests.system.refresh --dry-run
    python -m tests.system.refresh
    python -m tests.system.refresh --report ipv4_rib --report ipv6_rib

Use it only for a change to the flattening that you have reviewed report by
report. A column that moves because a *getter* changed is exactly what
``test_release_matrix.py`` exists to catch, and refreshing would silently accept
it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from tests.system.replay import Recording, recording_paths

logger = logging.getLogger("refresh")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without rewriting the recordings",
    )
    parser.add_argument(
        "--report",
        action="append",
        metavar="NAME",
        help="Refresh only this report's tables; repeat for several. Keeps a "
        "change to one table's rendering from rewriting every golden",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    paths = recording_paths()
    if not paths:
        logger.error("no recordings found under tests/fixtures/releases/")
        return 1

    changed = 0
    for path in paths:
        recording = Recording.load(path)
        updates = []
        for name, report in recording.reports.items():
            if args.report and name not in args.report:
                continue
            columns, rows = recording.table(name)
            if columns == report.columns and rows == report.rows:
                continue
            updates.append(name)
            logger.info("%s/%s %s", recording.release, recording.node, name)
            if columns != report.columns:
                logger.info(
                    "  columns -%s +%s",
                    [c for c in report.columns if c not in columns],
                    [c for c in columns if c not in report.columns],
                )
            if rows != report.rows:
                logger.info("  rows %d -> %d", len(report.rows), len(rows))
            report.columns = columns
            report.rows = rows
        if not updates:
            continue
        changed += len(updates)
        if not args.dry_run:
            recording.save(path)

    verb = "would refresh" if args.dry_run else "refreshed"
    logger.info("%s %d golden table(s)", verb, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
