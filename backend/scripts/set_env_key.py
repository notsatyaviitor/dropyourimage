#!/usr/bin/env python
"""Set a single key in backend/.env without echoing its value.

    python scripts/set_env_key.py GEMINI_API_KEY

Reads the value from stdin so it never appears in shell history, in a command line, or in
process listings. Creates .env from .env.example on first run.

Use this rather than editing .env by hand in a shared terminal.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ENV = BACKEND / ".env"
EXAMPLE = BACKEND / ".env.example"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    name = argv[1]
    if not ENV.exists():
        if not EXAMPLE.exists():
            print(f"error: neither {ENV} nor {EXAMPLE} exists", file=sys.stderr)
            return 1
        ENV.write_text(EXAMPLE.read_text())
        print(f"created {ENV.name} from {EXAMPLE.name}")

    value = getpass.getpass(f"{name} (input hidden): ").strip()
    if not value:
        print("no value given; nothing changed", file=sys.stderr)
        return 1

    lines = ENV.read_text().splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{name}=") and not stripped.startswith("#"):
            # Preserve any trailing comment on the line.
            comment = ""
            if "#" in line:
                head, _, tail = line.partition("#")
                if head.rstrip().endswith("=") or "=" in head:
                    comment = "  # " + tail.strip()
            lines[i] = f"{name}={value}{comment}"
            replaced = True
            break

    if not replaced:
        lines.append(f"{name}={value}")

    ENV.write_text("\n".join(lines) + "\n")
    print(f"set {name} in {ENV.name} ({len(value)} chars, value not shown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
