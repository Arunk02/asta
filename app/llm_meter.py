"""One place that answers "what did that turn actually cost?".

Why this exists: every chat turn was already writing a `traces` row, but the CLI
paths passed literal zeros, so 65 of the first 67 rows recorded nothing. The
schema was right and the dashboard was real — it just reported on an empty
table, which is worse than having no dashboard, because it looks like coverage.

The CLIs do report usage; nobody was reading it. Claude Code's stream-json emits
a `usage` block on every assistant message and again on the final `result`.
This module normalises that into one shape, so `main` records the same fields
whichever brain answered and the numbers are comparable across them.

WEIGHTS, and why the naive total misleads:

    fresh input   1.00x
    cache write   1.25x   — a first turn is nearly all cache write
    cache read    0.10x   — cheap per token, but it is re-paid EVERY turn and
                            grows with the transcript, so over a long
                            conversation it quietly becomes the largest line
    output        5.00x   — by far the most expensive token you can emit

`effective()` collapses those into input-token-equivalents. It is the number to
watch: raw token counts make a cache-heavy turn look expensive when it is cheap,
and a chatty one look cheap when it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

# Relative to one fresh input token. Ratios hold across Claude tiers, so a
# change of model moves the absolute bill without invalidating a comparison.
W_INPUT = 1.00
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.10
W_OUTPUT = 5.00


@dataclass
class Usage:
    """One turn's token accounting, whatever produced it."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    #: False when these are char-count estimates rather than executor-reported
    #: numbers. A trend line that mixes the two silently is a trend line that
    #: lies, so the distinction is stored, not just logged.
    measured: bool = False

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cost_usd=self.cost_usd + other.cost_usd,
            measured=self.measured or other.measured,
        )

    @property
    def effective(self) -> int:
        """Input-token-equivalents — the one number worth trending."""
        return int(self.input * W_INPUT
                   + self.cache_write * W_CACHE_WRITE
                   + self.cache_read * W_CACHE_READ
                   + self.output * W_OUTPUT)

    @property
    def total(self) -> int:
        """Raw tokens moved, unweighted. Useful for sanity, not for cost."""
        return self.input + self.output + self.cache_read + self.cache_write

    def as_dict(self) -> dict:
        d = asdict(self)
        d["effective"] = self.effective
        d["total"] = self.total
        return d


def from_anthropic(block: dict | None) -> Usage:
    """Parse an Anthropic-shaped `usage` object.

    Covers Claude Code stream-json, the Messages API, and PydanticAI's usage
    object once it has been dumped to a dict — they all use these key names.
    Unknown keys are ignored rather than raising: a provider adding a field
    must never be able to break the turn it is measuring.
    """
    if not isinstance(block, dict):
        return Usage()
    return Usage(
        input=int(block.get("input_tokens") or 0),
        output=int(block.get("output_tokens") or 0),
        cache_read=int(block.get("cache_read_input_tokens")
                       or block.get("cache_read_tokens") or 0),
        cache_write=int(block.get("cache_creation_input_tokens")
                        or block.get("cache_write_tokens") or 0),
        measured=True,
    )


#: Rough, and deliberately so — it exists to keep an unmeasured brain from
#: reading as free, not to price it. Anything using this is flagged
#: measured=False so it can be excluded from comparisons.
CHARS_PER_TOKEN = 4


def estimated(prompt_chars: int, reply_chars: int) -> Usage:
    """Last resort for a brain that reports nothing (Copilot CLI today)."""
    return Usage(
        input=prompt_chars // CHARS_PER_TOKEN,
        output=reply_chars // CHARS_PER_TOKEN,
        measured=False,
    )
