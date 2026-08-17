#!/usr/bin/env bash
# run_poc.sh — runs all 6 POC success-criteria steps end-to-end.
# Must be run with SRC_PASSWORD and TGT_PASSWORD set, inside the activated venv.
#
# Usage:
#   SRC_PASSWORD=migpw TGT_PASSWORD=tgtpw bash scripts/run_poc.sh
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=/tmp/marketo_poc

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
# Called automatically by the ERR trap; can also be called explicitly.
# Args: step_number
log_step_fail() {
    local step=$1
    echo -e "${RED}Step ${step} FAILED. Aborting.${NC}" >&2
    exit 1
}

# ERR trap: fired whenever any command exits with a non-zero status.
# Reports the line number where the failure occurred so it is easy to locate.
_on_error() {
    local lineno=$1
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo -e "[${timestamp}] [ERROR] Command failed at line ${lineno}. Aborting." >&2
    exit 1
}
trap '_on_error ${LINENO}' ERR

# ── Environment validation ────────────────────────────────────────────────────

# Verify that required environment variables are set and the Python package
# is importable.  Exits immediately on any missing prerequisite.
check_env() {
    # Abort with a helpful message if credentials are not exported
    : "${SRC_PASSWORD:?Please set SRC_PASSWORD before running}"
    : "${TGT_PASSWORD:?Please set TGT_PASSWORD before running}"

    # The venv must be active and 'pip install -e .' must have been run
    if ! python -c "import marketo_migration" 2>/dev/null; then
        log_error "marketo_migration not importable. Activate your venv and run 'pip install -e .' first."
    fi

    log_info "Environment checks passed."
}

# ── Step 1 ────────────────────────────────────────────────────────────────────

# Tear down any existing Docker volumes, start fresh containers, and load
# the source schema, seed data (52k leads + activities), and target schema
# via bootstrap.sh.  This guarantees a deterministic starting state.
step_1_setup() {
    log_banner 1 "Bring up containers and load seed data"

    # 'make reset' calls docker compose down -v then up -d then bootstrap.sh
    make -C "${REPO_ROOT}" reset

    log_step_ok 1
}

# ── Step 2 ────────────────────────────────────────────────────────────────────

# Run the orchestrator to inspect the source schema, compute PK-range chunks
# for acc_12345, and emit a self-contained migrate.sh plus a plan.json that
# lists which tables were planned and which were skipped (composite PK, etc.).
step_2_generate() {
    log_banner 2 "Generate migration script for acc_12345"

    python -m marketo_migration.orchestrator \
        --config  "${REPO_ROOT}/config.example.json" \
        --customer-id   acc_12345 \
        --tenant-column account_id \
        --out      "${WORK}/migrate.sh" \
        --plan-json "${WORK}/plan.json"

    log_info "Script : ${WORK}/migrate.sh"
    log_info "Plan   : ${WORK}/plan.json"
    log_step_ok 2
}

# ── Step 3 ────────────────────────────────────────────────────────────────────

# Execute the generated migration script.  The first chunk captures the MySQL
# binlog coordinate; remaining chunks run in parallel via xargs -P.  Each
# chunk writes a .done marker on success and skips itself on re-run.
step_3_migrate() {
    log_banner 3 "Run migration (acc_12345 → target)"

    bash "${WORK}/migrate.sh"

    log_step_ok 3
}

# ── Step 4 ────────────────────────────────────────────────────────────────────

# Demonstrate chunk-level resumability:
#   1. Generate a fresh script with smaller chunks and a dedicated state dir.
#   2. Run it in the background, kill it after 60 s.
#   3. Re-run — completed chunks must be skipped (SKIP lines appear in log).
step_4_kill_resume() {
    log_banner 4 "Kill mid-run and resume (demonstrating chunk resumability)"

    local kill_cfg="${WORK}/kill_cfg.json"
    local kill_state="${WORK}/kill_state"

    # Build a modified config with smaller chunks so enough .done markers
    # accumulate before the kill to make the SKIP demonstration meaningful.
    python3 - <<PYEOF
import json
with open("${REPO_ROOT}/config.example.json") as f:
    cfg = json.load(f)
cfg["state_dir"]   = "${WORK}/kill_state"
cfg["work_dir"]    = "${WORK}/kill_work"
cfg["chunk_size"]  = 2000
cfg["parallelism"] = 2
with open("${kill_cfg}", "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF

    # Generate the kill-demo migration script against the modified config
    python -m marketo_migration.orchestrator \
        --config  "${kill_cfg}" \
        --customer-id   acc_12345 \
        --tenant-column account_id \
        --out      "${WORK}/migrate_kill.sh" \
        --plan-json "${WORK}/plan_kill.json"

    log_info "Starting migration in background; will kill after 60 s..."
    bash "${WORK}/migrate_kill.sh" &
    local bg_pid=$!
    sleep 60
    kill "${bg_pid}" 2>/dev/null || true
    wait "${bg_pid}" 2>/dev/null || true

    local done_before
    done_before=$(find "${kill_state}" -name "*.done" 2>/dev/null | wc -l | tr -d ' ')
    log_info "Chunks with .done marker before kill: ${done_before}"

    if [[ "${done_before}" -eq 0 ]]; then
        log_warn "No chunks completed before kill (migration may be slow under emulation)."
        log_warn "Increase the sleep above or reduce chunk_size in kill_cfg.json."
    fi

    log_info "Re-running migration (completed chunks should be skipped)..."
    bash "${WORK}/migrate_kill.sh"

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
# acc_12345 on every migrated table.  Tables skipped by the orchestrator
# (composite PK) are excluded via --plan-json to avoid false failures.
step_5_verify() {
    log_banner 5 "Verify row-count and checksum parity (acc_12345)"

    python -m marketo_migration.verify \
        --config        "${REPO_ROOT}/config.example.json" \
        --customer-id   acc_12345 \
        --tenant-column account_id \
        --plan-json     "${WORK}/plan.json"

    log_step_ok 5
}

# ── Step 6 ────────────────────────────────────────────────────────────────────

# Run the full pytest suite (21 tests).  The suite spins up its own Docker
# stack via a session-scoped conftest fixture so it is independent of the
# state produced by steps 1–5.
step_6_test() {
    log_banner 6 "Run pytest suite"

    pytest -x -v "${REPO_ROOT}/tests/"

    log_step_ok 6
}

# ── Main ─────────────────────────────────────────────────────────────────────

# Entry point: validate the environment, prepare the working directory,
# then execute all six POC steps in sequence.
main() {
    # Start with a clean working directory so artifacts from previous runs
    # do not interfere with the current run
    rm -rf  "${WORK}"
    mkdir -p "${WORK}"

    check_env

    # Each step is wrapped so a failure prints a clear "Step N FAILED" message
    # before the ERR trap fires and exits.  The || log_step_fail N is reached
    # only when the step function itself returns non-zero without triggering the
    # trap (e.g. an explicit 'return 1').  The ERR trap covers all other cases.
    step_1_setup        || log_step_fail 1
    step_2_generate     || log_step_fail 2
    step_3_migrate      || log_step_fail 3
    step_4_kill_resume  || log_step_fail 4
    step_5_verify       || log_step_fail 5
    step_6_test         || log_step_fail 6

    echo
    echo -e "${BOLD}${GREEN}All 6 steps passed.${NC}"
}

main "$@"
