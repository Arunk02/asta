"""Asta.

.env is loaded here, at package import, rather than in the server entry point.
Every entry point needs it — the server, `python -m app.workspace`,
`python -m app.memory consolidate`, `python -m app.token_audit` — and when only
main.py loaded it, a CLI silently ran with different configuration than the
server. That surfaced as a workspace resolving to a weaker provider from the
command line than it did in the UI.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Real environment wins; .env only fills gaps.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
