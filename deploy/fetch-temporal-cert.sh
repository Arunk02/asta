#!/usr/bin/env bash
# Fetch a Temporal client certificate from Vault, safely.
#
# The documented one-liner is
#
#   vault kv get ... -field=TEMPORAL_CERT_PEM | base64 -d | openssl ... > preprod.pem
#
# and it has a trap in it: the shell creates and TRUNCATES the redirect target
# before vault runs. So a failed fetch — expired token, wrong path, no VPN —
# leaves a 0-byte file behind. That file then passes every `os.path.exists`
# check, and the failure surfaces much later from inside TLS as
#
#   tls: failed to find any PEM data in certificate input
#
# a sentence about PEM parsing that never mentions the empty file. That is
# exactly the state `preprod.pem` and `preprod.key` were found in.
#
# So this script writes to a temp file, VERIFIES it parses, and only then moves
# it into place. A failed fetch leaves whatever was there before untouched.
#
# Usage:  VAULT_ADDR=https://<your-vault> ./deploy/fetch-temporal-cert.sh preprod
set -euo pipefail

ENV_NAME="${1:-}"
PROXY="${ASTA_TEMPORAL_PROXY:-$HOME/temporal-mcp-proxy.py}"
CONFIG_DIR="${TEMPORAL_MCP_CONFIG:-$HOME/.config/temporal-mcp}"

if [ -z "$ENV_NAME" ]; then
  echo "usage: $0 <env>    (dev|qa|sit|uat|perf|preprod|prod)" >&2
  exit 64
fi

# The env -> cert-name and vault-path mapping lives in the proxy. Read it rather
# than restating it here: a second copy is how a newly added env goes missing in
# one of them.
read -r CERT_NAME VAULT_ARGS < <(python3 - "$PROXY" "$ENV_NAME" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("_p", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
env = sys.argv[2]
if env not in mod.ENV_MAP:
    sys.exit(f"unknown env {env!r}; known: {', '.join(mod.ENV_MAP)}")
hint = mod.VAULT_HINT.get(env) or mod._NONPROD_HINT.format(env=env)
print(mod.ENV_MAP[env]["cert"], hint)
PY
)

CERT="$CONFIG_DIR/$CERT_NAME.pem"
KEY="$CONFIG_DIR/$CERT_NAME.key"
echo "env $ENV_NAME -> cert '$CERT_NAME'  (vault: $VAULT_ARGS)"

if [ -z "${VAULT_ADDR:-}" ]; then
  echo "VAULT_ADDR is not set — vault would try localhost:8200 and fail." >&2
  echo "Export your Vault address first, then re-run." >&2
  exit 78
fi

mkdir -p "$CONFIG_DIR"
TMP="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf '$TMP'" EXIT
umask 077

fetch() {   # fetch <vault-field> <openssl-subcommand> <destination>
  local field="$1" kind="$2" dest="$3" tmp="$TMP/$(basename "$dest")"
  # Every stage is checked, and nothing touches $dest until all of them pass.
  if ! vault kv get $VAULT_ARGS -field="$field" 2>/dev/null \
       | tr -d '\n' | base64 -d 2>/dev/null \
       | openssl "$kind" -inform DER -outform PEM > "$tmp" 2>/dev/null; then
    echo "  ✗ $field: fetch or decode failed — leaving $(basename "$dest") as it was" >&2
    return 1
  fi
  if [ ! -s "$tmp" ]; then
    echo "  ✗ $field: produced an EMPTY file — this is the failure that leaves a" >&2
    echo "      0-byte cert behind when the redirect is done in the shell." >&2
    return 1
  fi
  if ! openssl "$kind" -in "$tmp" -noout 2>/dev/null; then
    echo "  ✗ $field: the result is not a valid $kind — not installing it" >&2
    return 1
  fi
  mv "$tmp" "$dest"
  chmod 600 "$dest"
  echo "  ✓ $(basename "$dest") ($(wc -c <"$dest" | tr -d ' ') bytes)"
}

ok=0
fetch TEMPORAL_CERT_PEM x509 "$CERT" || ok=1
fetch TEMPORAL_CERT_KEY rsa  "$KEY"  || ok=1

if [ "$ok" -ne 0 ]; then
  echo "" >&2
  echo "Nothing was overwritten with a broken file. Common causes:" >&2
  echo "  - Vault token expired      -> vault login ..." >&2
  echo "  - Not on the corporate VPN -> connect and retry" >&2
  echo "  - Wrong path for this env  -> $VAULT_ARGS" >&2
  exit 1
fi

echo ""
echo "Expiry:"
openssl x509 -in "$CERT" -noout -enddate | sed 's/^/  /'
echo ""
echo "Verify Asta agrees:"
echo "  cd ~/help/asta && .venv/bin/python -c \\"
echo "    \"from app import diagnostics; print(diagnostics.summary())\""
