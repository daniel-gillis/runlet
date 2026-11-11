#!/usr/bin/env python3
"""Check documentation against the no-ai-slop patterns.

Implements the word- and sentence-level checks from
https://github.com/petergyang/no-ai-slop, plus the structural checks proposed in
that repository's open pull requests (announced roadmaps, restated-thesis closes,
legacy-versus-modern framing, freshness framing).

Use it as a gate, not an oracle. Pattern matches are prompts to inspect a line; a
match is not proof that the line is bad.

    python3 tools/slop_check.py            # report
    python3 tools/slop_check.py --fail     # exit 1 when a hard pattern is found
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Words that are almost always filler in this project's documentation. Terms of
# art are deliberately absent: "harness" and "robust" are correct in their own
# domains, and this project uses "harness" for a real thing.
BANNED = [
    "delve", "foster", "leverage", "utilise", "utilize", "facilitate", "empower",
    "streamline", "cutting-edge", "paradigm shift", "game changer", "tapestry",
    "realm", "beacon", "multifaceted", "meticulous", "intricate", "paramount",
    "transformative", "elevate", "supercharge", "ever-evolving", "seamless",
    "robust", "testament", "pivotal", "underscores", "showcasing", "highlighting",
]

PHRASES = [
    (r"\bhere('?s| is) (the thing|what i mean)\b", "throat-clearing opener"),
    (r"\blet me be clear\b", "throat-clearing opener"),
    (r"\bi'?ll be honest\b", "throat-clearing opener"),
    (r"\bit'?s worth noting\b", "empty phrase"),
    (r"\bit'?s important to note\b", "empty phrase"),
    (r"\bworth (reading|knowing|noting)\b", "interpretive metadiscourse"),
    (r"\bthat last part matters\b", "interpretive metadiscourse"),
    (r"\bthe key point is\b", "interpretive metadiscourse"),
    (r"\bin other words\b", "interpretive metadiscourse"),
    (r"\bat the end of the day\b", "empty phrase"),
    (r"\bwhen it comes to\b", "empty phrase"),
    (r"\bin today'?s\b", "freshness framing"),
    (r"\bin the age of\b", "freshness framing"),
    (r"\bthe reality is\b", "empty phrase"),
    (r"\bin order to\b", "empty phrase"),
    (r"\bgoing forward\b", "empty phrase"),
    (r"\bwhat (nobody|most people) (tells you|get wrong|miss)\b", "faux-insight setup"),
    (r"\bexperts agree\b", "weasel attribution"),
    (r"\bstudies show\b", "weasel attribution"),
    (r"\bmarks a pivotal moment\b", "importance puffery"),
    (r"\bplays a vital role\b", "importance puffery"),
    (r"\bsolidifies its position\b", "importance puffery"),
    (r"\bnot just .{1,40} but\b", "binary contrast"),
    (r"\b(this|it) is not .{1,40}\. (it|this) (is|'?s)\b", "binary contrast"),
    (r"\bare not .{1,40}\. they are\b", "binary contrast"),
    (r"\bthe question isn'?t\b", "binary contrast"),
    (r"\bthat'?s it\. that'?s the\b", "dramatic fragmentation"),
    (r"\bin conclusion\b", "summary-recap ending"),
    (r"^\s*(ultimately|overall),", "summary-recap ending"),
    (r"\b(legacy|old) (way|approach|method)s? (versus|vs\.?|to) modern\b", "legacy-versus-modern framing"),
    (r"\bthis (changes|will change) everything\b", "importance puffery"),
    (r"\bwhat if i told you\b", "rhetorical setup"),
]


def check(path: Path) -> list[tuple[int, str, str]]:
    findings: list[tuple[int, str, str]] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("<!--") or "slop_check:ignore" in line:
            continue
        for word in BANNED:
            if re.search(rf"\b{re.escape(word)}\b", line, re.IGNORECASE):
                findings.append((number, f"banned word: {word}", stripped[:100]))
        for pattern, label in PHRASES:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append((number, label, stripped[:100]))
        if "\u2014" in line and not re.search(r"^\s*\|.*\u2014.*\|\s*$", line):
            findings.append((number, "em dash", stripped[:100]))
        # Mid-sentence bold is formatting slop; bold at line start or in a table is not.
        for match in re.finditer(r"(\S) \*\*[^*]+\*\*", line):
            if match.group(1) not in "|>-*":
                findings.append((number, "mid-sentence bold", stripped[:100]))
                break
    return findings


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail", action="store_true", help="exit non-zero on findings")
    ap.add_argument("paths", nargs="*", default=[])
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.paths] or sorted(Path(".").rglob("*.md"))
    paths = [p for p in paths if ".git" not in str(p) and "node_modules" not in str(p)]

    total = 0
    for path in paths:
        findings = check(path)
        total += len(findings)
        for number, label, text in findings:
            print(f"{path}:{number}: {label}: {text}")
    print(f"\n{total} finding(s) across {len(paths)} file(s)")
    if args.fail and total:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
