#!/usr/bin/env bash
# bootstrap.sh — bring up containers (if not running) and load fixtures.
# All mysql commands run inside containers via docker exec so the host's
# MySQL client version is irrelevant.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC_CONTAINER="marketo-src"
TGT_CONTAINER="marketo-tgt"
ROOT_PW="rootpw"

# ── Logging functions ─────────────────────────────────────────────────────────

# Print an informational message prefixed with an ISO-8601 timestamp.
log_info() {
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo "[${timestamp}] [INFO]  $*"
}

# Print a warning message with timestamp to stderr (non-fatal).
log_warn() {
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo "[${timestamp}] [WARN]  $*" >&2
}

# Print an error message with timestamp to stderr and abort the script.
log_error() {
    local timestamp
    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')
    echo "[${timestamp}] [ERROR] $*" >&2
    exit 1
}

# ── Helper functions ──────────────────────────────────────────────────────────

# Poll a MySQL container until it responds to SELECT 1 three consecutive times.
# Three consecutive successes are required to survive the MySQL Docker
# two-phase initialisation restart (the socket briefly disappears between
# the init phase and the final startup phase on a fresh volume).
# Args: container_name  display_label
_wait_ready() {
    local container=$1
    local label=$2
    local i=0
    local consec=0

    log_info "Waiting for ${label} (container: ${container})..."

    while (( consec < 3 )); do
        if docker exec -e "MYSQL_PWD=${ROOT_PW}" "${container}" \
                mysql -u root --connect-timeout=3 -sN -e "SELECT 1" &>/dev/null; then
            # Increment the consecutive-success counter
            consec=$(( consec + 1 ))
        else
            # Reset counter on any failure so we need 3 in a row
            consec=0
        fi

        i=$(( i + 1 ))
        if (( i > 90 )); then
            log_error "${label} not ready after 90 s"
        fi
        sleep 1
    done

    log_info "${label} is ready."
}

# Execute a SQL file inside a Docker container by piping it through stdin.
# The database argument is optional; pass an empty string to omit it.
# Args: container  user  password  database  sql_file_path
_exec_sql_file() {
    local container=$1
    local user=$2
    local pw=$3
    local db=$4
    local file=$5

    # Build the docker exec argument list dynamically so the database
    # name is only appended when it is non-empty
    local args=(-e "MYSQL_PWD=${pw}" -i "${container}" mysql -u "${user}")
    [[ -n "${db}" ]] && args+=("${db}")

    docker exec "${args[@]}" < "${file}"
}

# Execute an arbitrary SQL statement inside a Docker container as root.
# Suitable for GRANT / FLUSH PRIVILEGES / DDL statements.
# Args: container  sql_statement
_root_exec() {
    local container=$1
    shift
    docker exec -e "MYSQL_PWD=${ROOT_PW}" "${container}" mysql -u root -sN -e "$@"
}

# ── Main ─────────────────────────────────────────────────────────────────────

# Entry point: bring containers up, wait until both are stable, grant the
# required replication and admin privileges, then load all SQL fixtures.
main() {
    # Start (or no-op if already running) both MySQL containers
    docker compose -f "${REPO_ROOT}/docker/compose.yaml" up -d

    _wait_ready "${SRC_CONTAINER}" "source"
    _wait_ready "${TGT_CONTAINER}" "target"

    log_info "Granting privileges..."

    # Source: miguser needs read + replication rights for mysqldump and CDC
    _root_exec "${SRC_CONTAINER}" \
        "GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT \
         ON *.* TO 'miguser'@'%'; FLUSH PRIVILEGES;"

    # Target: tgtuser needs SUPER to run SET SESSION sql_log_bin=0 during bulk load
    _root_exec "${TGT_CONTAINER}" \
        "GRANT SUPER ON *.* TO 'tgtuser'@'%'; FLUSH PRIVILEGES;"

    log_info "Loading source schema..."
    _exec_sql_file "${SRC_CONTAINER}" root "${ROOT_PW}" "" \
        "${REPO_ROOT}/fixtures/source_schema.sql"

    log_info "Loading seed data (may take 30-90 s under emulation)..."
    _exec_sql_file "${SRC_CONTAINER}" root "${ROOT_PW}" "" \
        "${REPO_ROOT}/fixtures/seed.sql"

    log_info "Loading target schema..."
    _exec_sql_file "${TGT_CONTAINER}" root "${ROOT_PW}" "" \
        "${REPO_ROOT}/fixtures/target_schema.sql"

    log_info "Bootstrap complete. Source row counts (approximate):"
    _root_exec "${SRC_CONTAINER}" \
        "SELECT CONCAT('  ', table_name, ': ~', table_rows, ' rows')
         FROM information_schema.tables
         WHERE table_schema = 'marketo_src'
         ORDER BY table_name;"
}

main "$@"
