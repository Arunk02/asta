"""Timestamps on every line Asta's own process writes to server.log.

The log had none. uvicorn's default formatters print "INFO:     127.0.0.1 …"
and library warnings reach stderr through Python's last-resort handler as a
bare message, so "Token refresh failed: 404" sixteen times said nothing about
WHEN — whether it was one bad morning or every start for a month. An incident
reconstructed from that log is a guess.

Applied at import of app.main, which uvicorn does AFTER configuring its own
logging, so this replaces uvicorn's formatters rather than racing them. Lines a
child process writes straight to the inherited stderr (the MCP servers' banners)
cannot be stamped from here and are left as they are.
"""

from __future__ import annotations

import logging
import sys

DATEFMT = "%Y-%m-%d %H:%M:%S"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def apply() -> None:
    """Stamp uvicorn's handlers and give the root logger a stamped handler."""
    try:
        from uvicorn.logging import AccessFormatter, DefaultFormatter
    except ImportError:                                  # pragma: no cover
        AccessFormatter = DefaultFormatter = None        # type: ignore[assignment]

    if DefaultFormatter is not None:
        default = DefaultFormatter(fmt="%(asctime)s %(levelprefix)s %(message)s",
                                   datefmt=DATEFMT, use_colors=False)
        access = AccessFormatter(
            fmt='%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            datefmt=DATEFMT, use_colors=False)
        for name in ("uvicorn", "uvicorn.error"):
            for h in logging.getLogger(name).handlers:
                h.setFormatter(default)
        for h in logging.getLogger("uvicorn.access").handlers:
            h.setFormatter(access)

    root = logging.getLogger()
    if not any(getattr(h, "_asta_stamped", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        handler._asta_stamped = True                     # type: ignore[attr-defined]
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    # Asta's own named loggers (asta.invoke, …) speak at INFO and above.
    logging.getLogger("asta").setLevel(logging.INFO)
