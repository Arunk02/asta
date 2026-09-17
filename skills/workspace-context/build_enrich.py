"""Extract the enrichment contract from SKILL.md — generated, never hand-written."""
from pathlib import Path

SKILL = Path("skills/workspace-context/SKILL.md")
OUT = Path("skills/workspace-context/ENRICH.md")
HEADER = """<!-- GENERATED from SKILL.md "Step 5b" — do not edit by hand.
     Regenerate: python skills/workspace-context/build_enrich.py
     Why this file exists: re-enriching an existing repo needs the capture
     contract and nothing else. SKILL.md is ~6.9k tokens and most of it is
     bootstrap material for a NEW repo; this section is ~0.8k. Reading the whole
     skill on every drift pass is ~6k tokens of waste per run. -->

# Enrichment contract

"""


def section(text: str) -> str:
    out, on = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("### Step 5b"):
            on = True
        elif line.startswith("### Step 6"):
            break
        if on:
            out.append(line)
    return "".join(out).rstrip() + "\n"


def build() -> str:
    return HEADER + section(SKILL.read_text())


if __name__ == "__main__":
    OUT.write_text(build())
    print(f"wrote {OUT} ({len(build())} chars, ~{len(build())//4} tokens)")
