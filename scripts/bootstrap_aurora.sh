#!/usr/bin/env bash
# bootstrap_aurora.sh — bring up the SOURCE Docker container and seed it,
# then prepare the Aurora TARGET database (schema load + privilege grants).
#
# SOURCE side: MySQL 5.7 Docker container (on-premise simulation, same as local POC).
# TARGET side: AWS Aurora MySQL 2.x (MySQL 5.7 compatible), accessed via direct TCP.
#
# Required environment variables:
#   SRC_PASSWORD   — password for miguser on the source container  (default: migpw)
#   TGT_PASSWORD   — password for the Aurora target user
#   AURORA_HOST    — Aurora cluster writer endpoint
#   AURORA_PORT    — Aurora port                                    (default: 3306)
#   AURORA_USER    — Aurora master user or rds_superuser account
#   AURORA_DB      — Target database name                          (default: marketo_tgt)
#
# Optional:
#   AURORA_SSL_CA  — Path to the RDS CA bundle for SSL verification.
#                    If unset, SSL is not configured (only safe on private VPC).
#
# Usage:
#   AURORA_HOST=your-cluster.cluster-xxx.us-east-1.rds.amazonaws.com \
#   AURORA_USER=admin \
#   TGT_PASSWORD=secretpw \
#   SRC_PASSWORD=migpw \
#   bash scripts/bootstrap_aurora.sh
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC_CONTAINER="marketo-src"
ROOT_PW="rootpw"

# Aurora target settings — sourced from environment with sensible defaults
AURORA_HOST="${AURORA_HOST:?AURORA_HOST env var is required}"
AURORA_PORT="${AURORA_PORT:-3306}"
AURORA_USER="${AURORA_USER:?AURORA_USER env var is required}"
AURORA_DB="${AURORA_DB:-marketo_tgt}"


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


# ── Source helpers (Docker) ───────────────────────────────────────────────────

# Poll the source MySQL Docker container until it accepts SELECT 1 three times
# in a row.  Three consecutive successes are required to survive the MySQL Docker
# two-phase initialisation restart (the server briefly goes away between phases).
# Args: container_name  display_label
_wait_src_ready() {
    local container=$1
    local label=$2
    local i=0
    local consec=0

    log_info "Waiting for ${label} (container: ${container})..."

    while (( consec < 3 )); do
        if docker exec -e "MYSQL_PWD=${ROOT_PW}" "${container}" \
                mysql -u root --connect-timeout=3 -sN -e "SELECT 1" &>/dev/null; then
            # Increment consecutive-success counter
            consec=$(( consec + 1 ))
        else
            # Any failure resets the counter — we need 3 in a row
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
# Args: container  user  password  sql_file_path
_src_exec_sql_file() {
    local container=$1
    local user=$2
    local pw=$3
    local file=$4

    log_info "  Loading ${file##*/} into ${container}..."
    docker exec -e "MYSQL_PWD=${pw}" -i "${container}" mysql -u "${user}" < "${file}"
}

# Execute an arbitrary SQL statement inside the source container as root.
# Used for GRANT / FLUSH PRIVILEGES statements.
# Args: sql_statement
_src_root_exec() {
    docker exec -e "MYSQL_PWD=${ROOT_PW}" "${SRC_CONTAINER}" mysql -u root -sN -e "$@"
}


# ── Target helpers (Aurora — direct TCP) ──────────────────────────────────────

# Build the mysql CLI options common to all Aurora target commands.
# Stores them in the TGT_MYSQL_OPTS variable for reuse.
# Respects AURORA_SSL_CA if set; otherwise connects without explicit SSL config.
_build_tgt_opts() {
    TGT_MYSQL_OPTS=(
        -h "${AURORA_HOST}"
        -P "${AURORA_PORT}"
        -u "${AURORA_USER}"
    )
    if [[ -n "${AURORA_SSL_CA:-}" ]]; then
        TGT_MYSQL_OPTS+=(--ssl-ca="${AURORA_SSL_CA}")
        log_info "SSL CA configured: ${AURORA_SSL_CA}"
    else
        log_warn "AURORA_SSL_CA not set — connecting without explicit SSL verification."
    fi
}

# Poll the Aurora endpoint until it accepts a SELECT 1 three times in a row.
# Uses a host-side mysql client (or the source container's client as fallback).
# Timeout: 60 seconds.
_wait_aurora_ready() {
    local i=0
    local consec=0

    log_info "Waiting for Aurora at ${AURORA_HOST}:${AURORA_PORT}..."

    while (( consec < 3 )); do
        if MYSQL_PWD="${TGT_PASSWORD}" mysql \
                "${TGT_MYSQL_OPTS[@]}" \
                --connect-timeout=5 \
                -sN -e "SELECT 1" &>/dev/null 2>&1; then
            consec=$(( consec + 1 ))
        else
            consec=0
        fi

        i=$(( i + 1 ))
        if (( i > 60 )); then
            log_error "Aurora at ${AURORA_HOST}:${AURORA_PORT} not ready after 60 s"
        fi
        sleep 1
    done

    log_info "Aurora is reachable."
}

# Execute a SQL file against Aurora using the host-side mysql client.
# Args: sql_file_path
_aurora_exec_sql_file() {
    local file=$1

    log_info "  Loading ${file##*/} into Aurora (${AURORA_DB})..."
    MYSQL_PWD="${TGT_PASSWORD}" mysql \
        "${TGT_MYSQL_OPTS[@]}" \
        "${AURORA_DB}" \
        < "${file}"
}

# Execute an arbitrary SQL statement against Aurora as the configured user.
# Used for GRANT and DDL statements.
# Args: sql_statement
_aurora_exec() {
    MYSQL_PWD="${TGT_PASSWORD}" mysql \
        "${TGT_MYSQL_OPTS[@]}" \
        -sN -e "$@"
}

# Create the target database on Aurora if it does not already exist.
_aurora_create_db() {
    log_info "Creating database ${AURORA_DB} on Aurora (if not exists)..."
    MYSQL_PWD="${TGT_PASSWORD}" mysql \
        "${TGT_MYSQL_OPTS[@]}" \
        -sN -e "CREATE DATABASE IF NOT EXISTS \`${AURORA_DB}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
}

# Grant rds_superuser role to the configured Aurora user so it can execute
# SET SESSION sql_log_bin=0 during bulk load.  This is the Aurora MySQL 2.x
# equivalent of GRANT SUPER ON *.* — the SUPER privilege does not exist on Aurora.
#
# Note: This command requires the calling user to be the RDS master user.
# If the AURORA_USER is already the master user, this is a no-op.
_aurora_grant_privileges() {
    log_info "Granting rds_superuser to ${AURORA_USER} on Aurora..."
    MYSQL_PWD="${TGT_PASSWORD}" mysql \
        "${TGT_MYSQL_OPTS[@]}" \
        -sN -e "GRANT rds_superuser TO '${AURORA_USER}'@'%';" 2>/dev/null \
        || log_warn "Could not grant rds_superuser (user may already have it or lack GRANT OPTION)."
}


# ── Main ──────────────────────────────────────────────────────────────────────

# Entry point: bring up source Docker container, wait for both source and Aurora
# to be ready, load schemas and seed data, and verify connectivity.
main() {
    log_info "=== Bootstrap: Source (Docker) + Target (Aurora MySQL) ==="
    log_info "Aurora endpoint : ${AURORA_HOST}:${AURORA_PORT}"
    log_info "Aurora user     : ${AURORA_USER}"
    log_info "Target database : ${AURORA_DB}"

    # Build Aurora mysql CLI options once (respects AURORA_SSL_CA if set)
    _build_tgt_opts

    # ── Source: start Docker container and wait for it to be healthy ──────────
    log_info "Starting source Docker container..."
    docker compose -f "${REPO_ROOT}/docker/compose.yaml" up -d "${SRC_CONTAINER}"

    _wait_src_ready "${SRC_CONTAINER}" "source"

    # ── Source: grant replication and read privileges ─────────────────────────
    log_info "Granting source privileges to miguser..."
    _src_root_exec \
        "GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT \
         ON *.* TO 'miguser'@'%'; FLUSH PRIVILEGES;"

    # ── Source: load schema and seed data ─────────────────────────────────────
    log_info "Loading source schema..."
    _src_exec_sql_file "${SRC_CONTAINER}" root "${ROOT_PW}" \
        "${REPO_ROOT}/fixtures/source_schema.sql"

    log_info "Loading seed data (may take 30-90 s under emulation)..."
    _src_exec_sql_file "${SRC_CONTAINER}" root "${ROOT_PW}" \
        "${REPO_ROOT}/fixtures/seed.sql"

    # ── Target: verify Aurora connectivity ────────────────────────────────────
    _wait_aurora_ready

    # ── Target: create database and load schema ───────────────────────────────
    _aurora_create_db

    log_info "Granting rds_superuser privilege on Aurora..."
    _aurora_grant_privileges

    log_info "Loading target schema into Aurora..."
    _aurora_exec_sql_file "${REPO_ROOT}/fixtures/target_schema.sql"

    # ── Smoke-test: print source row counts ───────────────────────────────────
    log_info "Bootstrap complete. Source row counts (approximate):"
    _src_root_exec \
        "SELECT CONCAT('  ', table_name, ': ~', table_rows, ' rows')
         FROM information_schema.tables
         WHERE table_schema = 'marketo_src'
         ORDER BY table_name;"

    log_info "=== Bootstrap finished successfully ==="
}

main "$@"
