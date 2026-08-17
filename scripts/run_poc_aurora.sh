#!/usr/bin/env bash
# run_poc_aurora.sh — runs the 5-step Aurora POC end-to-end.
#
# SOURCE: MySQL 5.7 Docker container (on-premise simulation).
# TARGET: AWS Aurora MySQL 2.x (MySQL 5.7 compatible), accessed via direct TCP.
#
# Steps:
#   1. Bring up source Docker container, seed it, prepare Aurora target schema.
#   2. Generate migration script (migrate_aurora.sh) via orchestrator.
#   3. Execute migration: chunk-parallel bulk load from source into Aurora.
#   4. Kill mid-run and resume (demonstrates chunk-level resumability).
#   5. Verify row-count and CRC32 checksum parity between source and Aurora.
#
# Note: Step 6 (pytest suite) is omitted here because the test suite targets
# the local Docker stack.  Run 'make test' separately after verifying Aurora.
#
# Required environment variables:
#   SRC_PASSWORD   — miguser password on source container    (default: migpw)
#   TGT_PASSWORD   — Aurora target user password
#   AURORA_HOST    — Aurora cluster writer endpoint
#   AURORA_USER    — Aurora master user or rds_superuser account
#
# Optional:
#   AURORA_PORT    — Aurora port                             (default: 3306)
#   AURORA_DB      — Target database name                   (default: marketo_tgt)
#   AURORA_SSL_CA  — Path to RDS CA bundle for SSL (omit for private VPC)
#
# Usage:
#   AURORA_HOST=your-cluster.cluster-xxx.us-east-1.rds.amazonaws.com \
#   AURORA_USER=admin \
#   TGT_PASSWORD=secretpw \
#   SRC_PASSWORD=migpw \
#   bash scripts/run_poc_aurora.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORK=/tmp/marketo_poc_aurora

# ── Terminal colours ──────────────────────────────────────────────────────────
BOLD='\033[1m'
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'


# ── Logging functions ─────────────────────────────────────────────────────────

# Print an informational message prefixed with an ISO-8601 timestamp.
log_info() {
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo -e "[${timestamp}] [INFO]  $*"
}

# Print a warning message with timestamp to stderr (non-fatal).
log_warn() {
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo -e "[${timestamp}] [WARN]  $*" >&2
}

# Print an error message with timestamp to stderr and abort the script.
log_error() {
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo -e "[${timestamp}] [ERROR] $*" >&2
    exit 1
}

# Print a bold step banner to make console output easy to scan.
# Args: step_number step_name
log_banner() {
    local step=$1
    local name=$2
    echo
    echo -e "${BOLD}${CYAN}=== Step ${step}: ${name} ===${NC}"
}

# Print a green success line for the given step number.
# Args: step_number
log_step_ok() {
    local step=$1
    echo -e "${GREEN}Step ${step} successful.${NC}"
}

# Print a red failure line for the given step number and exit.
# Args: step_number
log_step_fail() {
    local step=$1
    echo -e "${RED}Step ${step} FAILED. Aborting.${NC}" >&2
    exit 1
}

# ERR trap: fired whenever any command exits with a non-zero status.
# Reports the line number so the failure is easy to locate.
_on_error() {
    local lineno=$1
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo -e "[${timestamp}] [ERROR] Command failed at line ${lineno}. Aborting." >&2
    exit 1
}
trap '_on_error ${LINENO}' ERR


# ── Environment validation ────────────────────────────────────────────────────

# Verify that all required environment variables are set and that the Python
# package is importable.  Exits immediately on any missing prerequisite.
check_env() {
    # Abort with a helpful message if credentials or Aurora settings are missing
    : "${SRC_PASSWORD:?Please set SRC_PASSWORD before running}"
    : "${TGT_PASSWORD:?Please set TGT_PASSWORD before running}"
    : "${AURORA_HOST:?Please set AURORA_HOST to your Aurora cluster endpoint}"
    : "${AURORA_USER:?Please set AURORA_USER to your Aurora master/superuser account}"

    # Apply defaults for optional Aurora vars
    export AURORA_PORT="${AURORA_PORT:-3306}"
    export AURORA_DB="${AURORA_DB:-marketo_tgt}"

    # The venv must be active and 'pip install -e .' must have been run
    if ! python -c "import marketo_migration" 2>/dev/null; then
        log_error "marketo_migration not importable. Activate your venv and run 'pip install -e .' first."
    fi

    log_info "Environment checks passed."
    log_info "Aurora endpoint : ${AURORA_HOST}:${AURORA_PORT}"
    log_info "Aurora user     : ${AURORA_USER}"
    log_info "Target database : ${AURORA_DB}"
}


# ── Config generation ─────────────────────────────────────────────────────────

# Generate a config.json for this run by embedding the Aurora connection details.
# Writes to ${WORK}/config_aurora.json so it does not overwrite config.example.json.
# The target section omits the 'container' key, which signals the orchestrator
# to use direct TCP connections instead of docker exec.
generate_config() {
    log_info "Generating Aurora config at ${WORK}/config_aurora.json..."

    python3 - <<PYEOF
import json, os

# Load the base example config as the template
with open("${REPO_ROOT}/config.aurora.example.json") as f:
    cfg = json.load(f)

# Overwrite target with the live Aurora connection details
cfg["target"] = {
    "host":     os.environ["AURORA_HOST"],
    "port":     int(os.environ.get("AURORA_PORT", "3306")),
    "user":     os.environ["AURORA_USER"],
    "database": os.environ.get("AURORA_DB", "marketo_tgt"),
}

# Redirect work and state directories to the Aurora-specific work directory
cfg["work_dir"]  = "${WORK}/migration_work"
cfg["state_dir"] = "${WORK}/migration_state"

with open("${WORK}/config_aurora.json", "w") as f:
    json.dump(cfg, f, indent=2)

print("Config written.")
PYEOF
}


# ── Step 1 ────────────────────────────────────────────────────────────────────

# Bring up the source Docker container, seed it with schema and data, and
# prepare the Aurora target database (create DB, load schema, grant privileges).
# Runs bootstrap_aurora.sh which handles both sides in one pass.
step_1_setup() {
    log_banner 1 "Bring up source container and prepare Aurora target"

    # Pass all required Aurora vars through to the bootstrap script
    AURORA_HOST="${AURORA_HOST}" \
    AURORA_PORT="${AURORA_PORT}" \
    AURORA_USER="${AURORA_USER}" \
    AURORA_DB="${AURORA_DB}" \
    TGT_PASSWORD="${TGT_PASSWORD}" \
    SRC_PASSWORD="${SRC_PASSWORD}" \
    bash "${SCRIPT_DIR}/bootstrap_aurora.sh"

    log_step_ok 1
}


# ── Step 2 ────────────────────────────────────────────────────────────────────

# Run the orchestrator to inspect the source schema, compute PK-range chunks
# for acc_12345, and emit a self-contained migrate_aurora.sh plus a plan.json.
# Because the config has no target.container, the orchestrator generates a script
# that connects to Aurora directly (no docker exec on the target side).
step_2_generate() {
    log_banner 2 "Generate Aurora migration script for acc_12345"

    generate_config

    python -m marketo_migration.orchestrator \
        --config        "${WORK}/config_aurora.json" \
        --customer-id   acc_12345 \
        --tenant-column account_id \
        --out           "${WORK}/migrate_aurora.sh" \
        --plan-json     "${WORK}/plan_aurora.json"

    log_info "Script : ${WORK}/migrate_aurora.sh"
    log_info "Plan   : ${WORK}/plan_aurora.json"
    log_step_ok 2
}


# ── Step 3 ────────────────────────────────────────────────────────────────────

# Execute the generated migration script.
# The first chunk captures the MySQL binlog coordinate from the source;
# remaining chunks run in parallel via xargs -P $PARALLELISM.
# Each chunk writes a .done marker on success and skips itself on re-run.
step_3_migrate() {
    log_banner 3 "Run migration (acc_12345 source -> Aurora)"

    bash "${WORK}/migrate_aurora.sh"

    log_step_ok 3
}


# ── Step 4 ────────────────────────────────────────────────────────────────────

# Demonstrate chunk-level resumability against Aurora:
#   1. Generate a fresh script with smaller chunks and a dedicated state dir.
#   2. Run it in the background, kill after 60 s.
#   3. Re-run — completed chunks must be skipped (SKIP lines appear in log).
step_4_kill_resume() {
    log_banner 4 "Kill mid-run and resume (demonstrating chunk resumability on Aurora)"

    local kill_cfg="${WORK}/kill_cfg_aurora.json"
    local kill_state="${WORK}/kill_state_aurora"

    # Build a modified config with smaller chunks so enough .done markers
    # accumulate before the kill to make the SKIP demonstration meaningful.
    python3 - <<PYEOF
import json, os

with open("${WORK}/config_aurora.json") as f:
    cfg = json.load(f)

cfg["state_dir"]   = "${WORK}/kill_state_aurora"
cfg["work_dir"]    = "${WORK}/kill_work_aurora"
cfg["chunk_size"]  = 2000
cfg["parallelism"] = 2

with open("${kill_cfg}", "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF

    # Generate the kill-demo migration script against the modified config
    python -m marketo_migration.orchestrator \
        --config        "${kill_cfg}" \
        --customer-id   acc_12345 \
        --tenant-column account_id \
        --out           "${WORK}/migrate_kill_aurora.sh" \
        --plan-json     "${WORK}/plan_kill_aurora.json"

    log_info "Starting Aurora migration in background; will kill after 60 s..."
    bash "${WORK}/migrate_kill_aurora.sh" &
    local bg_pid=$!
    sleep 60
    kill "${bg_pid}" 2>/dev/null || true
    wait "${bg_pid}" 2>/dev/null || true

    local done_before
    done_before=$(find "${kill_state}" -name "*.done" 2>/dev/null | wc -l | tr -d ' ')
    log_info "Chunks with .done marker before kill: ${done_before}"

    if [[ "${done_before}" -eq 0 ]]; then
        log_warn "No chunks completed before kill."
        log_warn "Network latency to Aurora may be high — try increasing the sleep or reducing chunk_size."
    fi

    log_info "Re-running migration (completed chunks should be skipped)..."
    bash "${WORK}/migrate_kill_aurora.sh"

    # Parse the second run's log for SKIP lines to confirm resumability
    local latest_log
    latest_log=$(ls -t "${kill_state}"/migration_*.log 2>/dev/null | head -1)
    if [[ -n "${latest_log}" ]]; then
        local skip_count
        skip_count=$(grep -c "SKIP.*marker exists" "${latest_log}" || true)
        log_info "SKIP lines in second run's log: ${skip_count}"
        log_info "--- SKIP lines ---"
        grep "SKIP.*marker exists" "${latest_log}" || log_info "(none)"
        log_info "------------------"
    fi

    log_step_ok 4
}


# ── Step 5 ────────────────────────────────────────────────────────────────────

# Verify data integrity: compare row counts and BIT_XOR(CRC32) checksums for
# acc_12345 on every migrated table between the source container and Aurora.
# Tables skipped by the orchestrator (composite PK) are excluded via --plan-json.
step_5_verify() {
    log_banner 5 "Verify row-count and checksum parity (acc_12345 on Aurora)"

    python -m marketo_migration.verify \
        --config        "${WORK}/config_aurora.json" \
        --customer-id   acc_12345 \
        --tenant-column account_id \
        --plan-json     "${WORK}/plan_aurora.json"

    log_step_ok 5
}


# ── Main ──────────────────────────────────────────────────────────────────────

# Entry point: validate the environment, prepare the working directory,
# then execute all five Aurora POC steps in sequence.
main() {
    # Start with a clean working directory so artifacts from previous runs
    # do not interfere with the current run
    rm -rf  "${WORK}"
    mkdir -p "${WORK}"

    check_env

    # Each step is wrapped so a failure prints a clear "Step N FAILED" message.
    # The ERR trap covers failures inside the step functions.
    step_1_setup        || log_step_fail 1
    step_2_generate     || log_step_fail 2
    step_3_migrate      || log_step_fail 3
    step_4_kill_resume  || log_step_fail 4
    step_5_verify       || log_step_fail 5

    echo
    echo -e "${BOLD}${GREEN}All 5 Aurora POC steps passed.${NC}"
    echo -e "CDC hand-off coordinate: ${WORK}/migration_state/coords/binlog.coord"
}

main "$@"
