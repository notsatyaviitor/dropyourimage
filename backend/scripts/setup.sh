#!/usr/bin/env bash
# Creates the backend virtualenv and installs dependencies. Idempotent — re-run after a
# requirements.txt change.
#
#   cd backend && ./scripts/setup.sh && source .venv/bin/activate
#
# requirements.txt now includes pytoshop (the PSD writer), so this installs everything needed.
# requirements-psd-fallback.txt is SUPERSEDED — do not install it, see the note in that file.

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BACKEND_DIR"

VENV=".venv"
PY_MIN_MINOR=10

# --- interpreter check ---------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found" >&2
  exit 1
fi

minor="$(python3 -c 'import sys; print(sys.version_info.minor)')"
if (( minor < PY_MIN_MINOR )); then
  echo "error: Python 3.${PY_MIN_MINOR}+ required, found 3.${minor}" >&2
  exit 1
fi

# --- libmagic (python-magic is only a binding) ---------------------------------------
# Not fatal: MIME sniffing degrades to a warning rather than blocking setup, but zip
# ingest hardening depends on it. See docs/SECURITY.md.
if ! ldconfig -p 2>/dev/null | grep -q 'libmagic\.so'; then
  cat >&2 <<'EOF'
warning: libmagic not found. python-magic will fail to load at runtime.
         Install it:  sudo apt-get install -y libmagic1
         Zip-entry MIME sniffing is a security control, so do not skip this before
         handling untrusted uploads.
EOF
fi

# --- venv ----------------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
  echo "==> creating $VENV"
  python3 -m venv "$VENV"
else
  echo "==> reusing existing $VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> upgrading pip"
python -m pip install --quiet --upgrade pip setuptools wheel

echo "==> installing requirements.txt"
python -m pip install -r requirements.txt

# --- verify --------------------------------------------------------------------------
echo "==> verifying imports"
python - <<'EOF'
import importlib, sys

REQUIRED = [
    ("numpy", "pixel maths"),
    ("cv2", "contours and morphology"),
    ("PIL", "decode/encode, ICC"),
    ("fastapi", "API"),
    ("pydantic", "contract"),
    ("rq", "queue"),
    ("redis", "queue"),
    ("httpx", "vendor HTTP"),
    ("boto3", "object storage"),
    ("psd_tools", "PSD validation"),
    ("pytest", "tests"),
]

missing = []
for mod, why in REQUIRED:
    try:
        importlib.import_module(mod)
    except Exception as exc:          # noqa: BLE001 - report anything, including load errors
        missing.append(f"  {mod:<12} ({why}): {type(exc).__name__}: {exc}")

# python-magic is reported separately: it imports only if libmagic is present on the system.
try:
    importlib.import_module("magic")
    magic_ok = True
except Exception:
    magic_ok = False

if missing:
    print("FAILED - unusable imports:", file=sys.stderr)
    print("\n".join(missing), file=sys.stderr)
    sys.exit(1)

print("all required imports OK")
if not magic_ok:
    print("NOTE: python-magic unavailable (libmagic missing) - install libmagic1 before "
          "accepting untrusted uploads")
EOF

cat <<'EOF'

Done. Next:

  source .venv/bin/activate
  pytest -q                 # imaging core: no API keys, no images required
  cp .env.example .env       # then fill in keys as they arrive

EOF
