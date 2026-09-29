"""How well does Asta read a conversation? Measured on his own messages.

Every case is a real 1:1 message from his Teams (26-29 Sep), plus the two
sentences he used to correct the design. Where a message can fairly be read two
ways, both readings are accepted — the score only counts readings that are
wrong, not ones that are arguable.

    .venv/bin/python -m app.understand_eval          # rules and the live model
    .venv/bin/python -m app.understand_eval --rules  # rules only, no model

The suite runs the rules half on every change (tests/test_round4_phase_d.py), so
the floor Asta falls back to can never quietly get worse.
"""

from __future__ import annotations

import asyncio
import re
import sys

#: (message, acceptable states). A trailing "N ... reaction." in a 1:1 is HIS
#: reaction, exactly as the sweep reads it.
CASES: list[tuple[str, set[str]]] = [
    ("Bro, please ping when free.", {"opener"}),
    ("need ur help", {"opener"}),
    ("Call me bro", {"ask", "opener"}),
    ("can you please merge this", {"ask"}),
    ("please review and approve once u login tomo", {"ask"}),
    ("Done Arun can you please check it", {"ask"}),
    ("Bro, let connect and finish helm now only?", {"ask"}),
    ("Ok Bro then only booking and servicePlan Avro right", {"ask"}),
    ("Thanks! also one more thing, can you check booking 88271?", {"ask"}),
    ("NAM CCD connection there is an error", {"ask", "urgent"}),
    ("Not seeing active consumers bro", {"ask", "urgent"}),
    ("also this failed", {"ask", "urgent"}),
    ("I have pushed changes to booking-service\n"
     "https://github.com/Maersk-Global/telikos-booking-service/pull/1456", {"ask", "status"}),
    ("Check from their code na", {"ask", "status"}),
    ("What kind of testing is this, bro? Without even testing the scenario, "
     "they are closing the defect.", {"ask", "status"}),
    ("We will connect post lunch and close the defect", {"status"}),
    ("but tunnels are not up yet", {"status"}),
    ("hence merging for release branches", {"status"}),
    ("mmm okay.. also I don't think there is any change required at EH", {"status", "closing"}),
    ("Lets merge tomorrow morning bro", {"status", "closing"}),
    ("Thank you bro", {"closing"}),
    ("perfect, that's all I needed, thanks!", {"closing"}),
    ("Yes bro", {"closing", "status"}),
    ("Haan bro", {"closing", "status"}),
    ("ohh okay\n\n1 Like reaction.", {"closing"}),
    ("It's not responding..\n\n👍\n\n1 Like reaction.", {"closing"}),
    ("Thank you\n\n1 Saluting face reaction.", {"closing"}),
    ("hence merging for release branches\n\n👍\n\n1 Like reaction.", {"closing"}),
]

_REACTION = re.compile(r"\n\s*\d+\s+[^\n]{1,40}?\breactions?\.?\s*$", re.I)


def items() -> list[dict]:
    return [{"id": f"eval:{i}", "who": "Colleague", "one_to_one": True,
             "new": [text], "so_far": "", "past": [], "asta_spoke": False,
             "handled_by_him": bool(_REACTION.search(text))}
            for i, (text, _) in enumerate(CASES)]


def score(decisions: dict[str, dict]) -> tuple[float, list[str]]:
    wrong = []
    for i, (text, ok) in enumerate(CASES):
        got = decisions[f"eval:{i}"]["state"]
        if got not in ok:
            wrong.append(f"{got:8} expected {'/'.join(sorted(ok)):14} {text[:60]!r}")
    return 1 - len(wrong) / len(CASES), wrong


def rules_only() -> tuple[float, list[str]]:
    from . import understand
    return score({it["id"]: understand.rules(it) for it in items()})


async def with_model() -> tuple[float, list[str]]:
    from . import understand
    return score(await understand.read(items()))


def main(argv: list[str]) -> int:
    acc, wrong = rules_only()
    print(f"rules  {acc:.0%}  ({len(CASES) - len(wrong)}/{len(CASES)})")
    for w in wrong:
        print("   ", w)
    if "--rules" not in argv:
        import os
        os.environ.setdefault("ASTA_UNDERSTAND_MODEL", "haiku")
        acc, wrong = asyncio.run(with_model())
        print(f"model  {acc:.0%}  ({len(CASES) - len(wrong)}/{len(CASES)})")
        for w in wrong:
            print("   ", w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
