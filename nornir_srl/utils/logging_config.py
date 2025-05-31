import logging
import sys
from typing import Optional, List


def setup_logging(level: str, log_file: Optional[str] = None) -> None:
    """Configure basic logging."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    # ensure Nornir logs use the same level
    logging.getLogger("nornir").setLevel(numeric_level)
