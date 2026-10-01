#!/usr/bin/env bash
# Deploy the full prototype in one run, in a new or existing resource group:
#
#   1. Resource group
#   2. Infrastructure (infra/main.bicep): Foundry + models, Storage, AI Search, App Insights, connections, and roles
#   3. .env with deployment outputs and Python environment (.venv)
#   4. Synthetic corpus -> Blob Storage (docs-dms / docs-bank)
#   5. AI Search indexes (indexed general documents + empty customer index)
#   6. Option 1 hosted agent (azd deploy) and identity roles
#   7. Option 2 prompt agent (File Search)
#   8. Smoke test with both agents
#
# Usage:
#   ./scripts/deploy.sh
#
# Optional variables (default value):
#   RESOURCE_GROUP=rg-foundry-docs-runtime   LOCATION=swedencentral   AZD_ENV_NAME=docs-poc
#   AZURE_SUBSCRIPTION_ID=<active one in az>
#   CHAT_MODEL=gpt-5.6-terra   CHAT_MODEL_VERSION=2026-07-09   CHAT_SKU=GlobalStandard   CHAT_CAPACITY=150
#   EMBEDDING_SKU=GlobalStandard   EMBEDDING_CAPACITY=150
#   TAGS='{"owner":"me"}'   additional tags (JSON) for the group and all resources
#   NAME_SUFFIX=<random>    resource-name suffix; generated on first run and kept in the group tag docs-name-suffix
#   PYTHON=python3.12       interpreter used to create .venv (3.10 to 3.13)
#   SKIP_SMOKE_TEST=1       skip the final smoke test
#
# Idempotent: if something fails (quota, role propagation...), fix it and rerun.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-foundry-docs-runtime}"
LOCATION="${LOCATION:-swedencentral}"
AZD_ENV_NAME="${AZD_ENV_NAME:-docs-poc}"
CHAT_MODEL="${CHAT_MODEL:-gpt-5.6-terra}"
CHAT_MODEL_VERSION="${CHAT_MODEL_VERSION:-2026-07-09}"
CHAT_SKU="${CHAT_SKU:-GlobalStandard}"
CHAT_CAPACITY="${CHAT_CAPACITY:-150}"
EMBEDDING_SKU="${EMBEDDING_SKU:-GlobalStandard}"
EMBEDDING_CAPACITY="${EMBEDDING_CAPACITY:-150}"
TAGS="${TAGS:-}"
[ -n "$TAGS" ] || TAGS='{}'
SKIP_SMOKE_TEST="${SKIP_SMOKE_TEST:-0}"
AGENT_SERVICE=docs-runtime-agent
STATE_DIR="$ROOT/.azure/deploy"

step() { printf '\n\033[1m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
info() { printf '  %s\n' "$*"; }
die() {
  printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2
  exit 1
}

# retry <tries> <wait_s> <command...>: recently assigned roles take a few minutes to propagate.
retry() {
  local tries=$1 wait=$2 n=1
  shift 2
  until "$@"; do
    [ "$n" -lt "$tries" ] || return 1
    info "... retry $n/$((tries - 1)) in ${wait} s (role/project propagation or agent startup)"
    sleep "$wait"
    n=$((n + 1))
  done
}

# ---------------------------------------------------------------------------------------------
step "0/8 Prerequisite checks"
# ---------------------------------------------------------------------------------------------
for bin in az azd; do
  command -v "$bin" >/dev/null || die "missing '$bin' in PATH (see 'Requirements' in the README)"
done

supported_python() { "$1" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))' 2>/dev/null; }
if [ -z "${PYTHON:-}" ]; then
  for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null && supported_python "$candidate"; then
      PYTHON="$candidate"
      break
    fi
  done
fi
if [ -z "${PYTHON:-}" ] && command -v uv >/dev/null; then
  info "Python 3.10-3.13 is not in PATH: obtaining it with uv"
  PYTHON="$(uv python find 3.12 2>/dev/null || { uv python install 3.12 >/dev/null && uv python find 3.12; })"
fi
[ -n "${PYTHON:-}" ] || die "Python 3.10-3.13 is required (install it or set PYTHON=/path/to/python3.12)"
supported_python "$PYTHON" || die "$PYTHON is $("$PYTHON" -V 2>&1); Python 3.10-3.13 is required"
info "Python: $("$PYTHON" -V 2>&1) ($PYTHON)"

az account show -o none 2>/dev/null || die "sign in with 'az login'"
azd auth login --check-status >/dev/null 2>&1 || die "sign in with 'azd auth login'"
if ! azd extension list --installed 2>/dev/null | grep -q '^azure\.ai\.agents '; then
  info "installing the azd azure.ai.agents extension"
  azd extension install azure.ai.agents --no-prompt
fi

SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
TENANT_ID="$(az account show --subscription "$SUBSCRIPTION_ID" --query tenantId -o tsv)"
if [ "$(az account show --query user.type -o tsv)" = "servicePrincipal" ]; then
  PRINCIPAL_TYPE=ServicePrincipal
  PRINCIPAL_ID="$(az ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv)"
else
  PRINCIPAL_TYPE=User
  PRINCIPAL_ID="$(az ad signed-in-user show --query id -o tsv)"
fi
info "Subscription: $(az account show --subscription "$SUBSCRIPTION_ID" --query name -o tsv) ($SUBSCRIPTION_ID)"
info "Deploying identity: $PRINCIPAL_TYPE $PRINCIPAL_ID"
mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------------------------------------
step "1/8 Resource group $RESOURCE_GROUP ($LOCATION)"
# ---------------------------------------------------------------------------------------------
RG_TAGS=()
while IFS= read -r tag; do
  RG_TAGS+=("$tag")
done < <("$PYTHON" -c 'import json, sys
tags = {"project": "foundry-docs-runtime-poc", "dataClassification": "synthetic", **json.loads(sys.argv[1])}
print("\n".join(f"{k}={v}" for k, v in tags.items()))' "$TAGS")
# Resource-name suffix, kept as a group tag so reruns reuse it. A new suffix after destroy.sh avoids
# recreating a just-purged Foundry account name: its endpoint can keep answering 404 for a long time.
NAME_SUFFIX="${NAME_SUFFIX:-$(az group show --subscription "$SUBSCRIPTION_ID" -n "$RESOURCE_GROUP" --query 'tags."docs-name-suffix"' -o tsv 2>/dev/null || true)}"
if [ -z "$NAME_SUFFIX" ]; then
  NAME_SUFFIX="$("$PYTHON" -c 'import secrets, string; print(secrets.choice(string.ascii_lowercase) + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(11)))')"
fi
RG_TAGS+=("docs-name-suffix=$NAME_SUFFIX")
az group create --subscription "$SUBSCRIPTION_ID" -n "$RESOURCE_GROUP" -l "$LOCATION" --tags "${RG_TAGS[@]}" -o none
info "resource-name suffix: $NAME_SUFFIX"

# ---------------------------------------------------------------------------------------------
step "2/8 Infrastructure (infra/main.bicep) · about 10 min, mostly AI Search"
# ---------------------------------------------------------------------------------------------
OUTPUTS="$STATE_DIR/outputs.json"
if ! az deployment group create --subscription "$SUBSCRIPTION_ID" -g "$RESOURCE_GROUP" \
  -n "docs-runtime-$(date +%Y%m%d-%H%M%S)" -f infra/main.bicep \
  -p principalId="$PRINCIPAL_ID" principalType="$PRINCIPAL_TYPE" suffix="$NAME_SUFFIX" \
  chatModelName="$CHAT_MODEL" chatModelVersion="$CHAT_MODEL_VERSION" chatModelSku="$CHAT_SKU" \
  chatModelCapacity="$CHAT_CAPACITY" embeddingModelSku="$EMBEDDING_SKU" embeddingModelCapacity="$EMBEDDING_CAPACITY" \
  tags="$TAGS" --query properties.outputs -o json >"$OUTPUTS"; then
  die "infra/main.bicep deployment failed. If this is quota-related, lower CHAT_CAPACITY or change CHAT_SKU / LOCATION and rerun."
fi

# ---------------------------------------------------------------------------------------------
step "3/8 .env and Python environment"
# ---------------------------------------------------------------------------------------------
# .env: read by scripts and the console (python-dotenv). azd.env: values for the azd environment.
"$PYTHON" - "$OUTPUTS" "$SUBSCRIPTION_ID" "$TENANT_ID" "$STATE_DIR/azd.env" <<'PY'
import json
import sys
from pathlib import Path

outputs, subscription, tenant, azd_env = sys.argv[1:]
values = {"AZURE_SUBSCRIPTION_ID": subscription, "AZURE_TENANT_ID": tenant}
values.update({k.upper(): v["value"] for k, v in json.loads(Path(outputs).read_text()).items()})
values["FOUNDRY_PROJECT_ENDPOINT"] = values["AZURE_AI_PROJECT_ENDPOINT"]


def dump(items: dict) -> str:
    return "".join(f'{k}="{v}"\n' for k, v in items.items())


Path(".env").write_text(
    "# Generated by scripts/deploy.sh from infra/main.bicep outputs.\n"
    + dump(values)
    + "# Customer documents (health data) must not be sent in telemetry.\n"
    + dump({"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false", "ENABLE_SENSITIVE_DATA": "false"})
)
azd_keys = [
    "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_LOCATION", "AZURE_RESOURCE_GROUP",
    "AZURE_AI_ACCOUNT_NAME", "AZURE_AI_PROJECT_NAME", "AZURE_AI_PROJECT_ID", "AZURE_AI_PROJECT_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "EMBEDDING_DEPLOYMENT_NAME", "STORAGE_ACCOUNT_URL", "SEARCH_ENDPOINT",
]
# Code deployment (no ACR); the account and project already exist: azd does not provision anything.
fixed = {"AZD_AGENT_SKIP_ACR": "true", "ENABLE_HOSTED_AGENTS": "true", "ENABLE_CAPABILITY_HOST": "false", "AI_PROJECT_DEPLOYMENTS": "[]"}
Path(azd_env).write_text(dump({**{k: values[k] for k in azd_keys}, **fixed}))
print(f"  .env with {len(values)} values · project {values['AZURE_AI_PROJECT_ENDPOINT']}")
PY

if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
PY=.venv/bin/python
"$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
info "dependencies installed in .venv"

# ---------------------------------------------------------------------------------------------
step "4/8 Synthetic corpus -> Blob Storage"
# ---------------------------------------------------------------------------------------------
retry 10 30 "$PY" scripts/upload_corpus.py

# ---------------------------------------------------------------------------------------------
step "5/8 AI Search indexes"
# ---------------------------------------------------------------------------------------------
retry 10 30 "$PY" scripts/index_general.py

# ---------------------------------------------------------------------------------------------
step "6/8 Hosted agent (Option 1) with azd · 2-4 min"
# ---------------------------------------------------------------------------------------------
if [ ! -f ".azure/$AZD_ENV_NAME/.env" ]; then
  azd env new "$AZD_ENV_NAME" --subscription "$SUBSCRIPTION_ID" --location "$LOCATION" --no-prompt
fi
azd env select "$AZD_ENV_NAME"
azd env set --file "$STATE_DIR/azd.env" -e "$AZD_ENV_NAME"
# A just-created project can answer "Project not found" (404) on its data plane for a few minutes.
retry 6 30 azd deploy "$AGENT_SERVICE" -e "$AZD_ENV_NAME" --no-prompt
retry 5 20 "$PY" scripts/grant_agent_rbac.py

# ---------------------------------------------------------------------------------------------
step "7/8 Prompt agent (Option 2): File Search + AI Search"
# ---------------------------------------------------------------------------------------------
retry 5 30 "$PY" scripts/filesearch_agent.py create
# Variant with customer_id filter: only isolation test F5 uses it (shared vector store)
retry 5 30 "$PY" scripts/filesearch_agent.py create-shared

# ---------------------------------------------------------------------------------------------
step "8/8 Smoke test"
# ---------------------------------------------------------------------------------------------
if [ "$SKIP_SMOKE_TEST" = "1" ]; then
  info "skipped (SKIP_SMOKE_TEST=1)"
else
  info "hosted agent: its identity roles may take a few minutes to apply"
  retry 8 60 "$PY" scripts/converse.py CLI-0003 --engine hosted --max 2 --min-ok 1
  retry 3 30 "$PY" scripts/converse.py CLI-0003 --engine filesearch --max 2 --min-ok 1
fi

step "Done"
cat <<EOF
  Foundry project:    $(grep '^AZURE_AI_PROJECT_ENDPOINT=' .env | cut -d'"' -f2)
  Demo console:       .venv/bin/python demo/server.py   →  http://127.0.0.1:8780
  CLI conversation:   .venv/bin/python scripts/converse.py CLI-0003 --engine hosted
  Isolation tests:    .venv/bin/python scripts/test_isolation.py
  Delete everything:  ./scripts/destroy.sh
EOF
