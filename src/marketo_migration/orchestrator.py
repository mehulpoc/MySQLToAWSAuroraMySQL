"""
Orchestrator: plans a migration for one customer and emits a self-contained
bash script plus a plan JSON.

Usage:
    python -m marketo_migration.orchestrator \\
        --config config.json \\
        --customer-id acc_12345 \\
        --tenant-column account_id \\
        --out migrate_acc_12345.sh \\
        --plan-json plan_acc_12345.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from typing import Optional

import pymysql
import pymysql.cursors


# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_logger = logging.getLogger(__name__)


def log_info(msg: str) -> None:
    """Log an informational message with ISO-8601 timestamp."""
    _logger.info(msg)


def log_warn(msg: str) -> None:
    """Log a warning message with ISO-8601 timestamp."""
    _logger.warning(msg)


def log_error(msg: str) -> None:
    """Log an error message with ISO-8601 timestamp."""
    _logger.error(msg)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments for the orchestrator.

    Returns the populated namespace with config path, customer ID,
    tenant column name, output script path, and output plan JSON path.
    """
    p = argparse.ArgumentParser(
        description="Generate a per-customer migration script."
    )
    p.add_argument("--config",         required=True, help="Path to config JSON")
    p.add_argument("--customer-id",    required=True, help="Tenant identifier to migrate")
    p.add_argument("--tenant-column",  required=True, help="Column holding the tenant ID")
    p.add_argument("--out",            required=True, help="Output bash script path")
    p.add_argument("--plan-json",      required=True, help="Output plan JSON path")
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config(path: str) -> dict:
    """
    Load and return the migration config from a JSON file.

    Args:
        path: Absolute or relative path to the config JSON file.

    Returns:
        Parsed config dictionary.
    """
    with open(path) as f:
        return json.load(f)


def _connect(endpoint: dict, password_env: str) -> pymysql.Connection:
    """
    Open a PyMySQL connection to the given endpoint.

    The password is read from the environment variable named by password_env
    so that it is never embedded in config files or command lines.

    Args:
        endpoint:     Dict with keys host, port, user, database.
        password_env: Name of the environment variable holding the password.

    Returns:
        An open pymysql.Connection using DictCursor.
    """
    password = os.environ.get(password_env, "")
    return pymysql.connect(
        host=endpoint["host"],
        port=int(endpoint["port"]),
        user=endpoint["user"],
        password=password,
        database=endpoint["database"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


# ── Schema introspection ──────────────────────────────────────────────────────

def _tables_with_tenant_col(
    conn: pymysql.Connection,
    database: str,
    tenant_column: str,
    include: list[str],
    exclude: list[str],
) -> list[str]:
    """
    Return the names of all BASE TABLEs that carry the tenant column.

    Applies the include and exclude filters from the config after the schema
    query so the caller can restrict the migration to a subset of tables.

    Args:
        conn:          Open source connection.
        database:      Source database name.
        tenant_column: Column name used as the tenant discriminator.
        include:       Whitelist of table names; empty list means all tables.
        exclude:       Blacklist of table names to skip.

    Returns:
        Filtered and sorted list of table names.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.TABLE_NAME
            FROM INFORMATION_SCHEMA.COLUMNS c
            JOIN INFORMATION_SCHEMA.TABLES t
              ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
             AND t.TABLE_NAME   = c.TABLE_NAME
             AND t.TABLE_TYPE   = 'BASE TABLE'
            WHERE c.TABLE_SCHEMA = %s
              AND c.COLUMN_NAME  = %s
            ORDER BY c.TABLE_NAME
            """,
            (database, tenant_column),
        )
        tables = [r["TABLE_NAME"] for r in cur.fetchall()]

    # Apply whitelist first, then blacklist
    if include:
        tables = [t for t in tables if t in include]
    if exclude:
        tables = [t for t in tables if t not in exclude]

    return tables


def _pk_info(
    conn: pymysql.Connection,
    database: str,
    table: str,
) -> tuple[Optional[str], bool, str]:
    """
    Inspect the primary key of a table and determine whether it is chunkable.

    A table is chunkable only if it has a single-column integer primary key.
    Composite PKs, non-integer PKs, and missing PKs all produce is_chunkable=False.

    Args:
        conn:     Open source connection.
        database: Source database name.
        table:    Table name to inspect.

    Returns:
        Tuple of (pk_col_name, is_chunkable, reason_string).
        pk_col_name is None when not chunkable.
        reason_string is empty when chunkable.
    """
    integer_types = {"tinyint", "smallint", "mediumint", "int", "bigint"}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT kcu.COLUMN_NAME, c.DATA_TYPE
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            JOIN INFORMATION_SCHEMA.COLUMNS c
              ON  c.TABLE_SCHEMA = kcu.TABLE_SCHEMA
              AND c.TABLE_NAME   = kcu.TABLE_NAME
              AND c.COLUMN_NAME  = kcu.COLUMN_NAME
            WHERE kcu.TABLE_SCHEMA    = %s
              AND kcu.TABLE_NAME      = %s
              AND kcu.CONSTRAINT_NAME = 'PRIMARY'
            ORDER BY kcu.ORDINAL_POSITION
            """,
            (database, table),
        )
        pk_cols = cur.fetchall()

    if not pk_cols:
        return None, False, "no primary key"

    if len(pk_cols) > 1:
        names = ", ".join(r["COLUMN_NAME"] for r in pk_cols)
        return None, False, f"composite PK ({names})"

    col = pk_cols[0]
    if col["DATA_TYPE"].lower() not in integer_types:
        return None, False, f"non-integer PK type={col['DATA_TYPE']}"

    return col["COLUMN_NAME"], True, ""


def _table_stats(
    conn: pymysql.Connection,
    database: str,
    table: str,
    pk_col: str,
    tenant_column: str,
    customer_id: str,
) -> Optional[tuple[int, int, int]]:
    """
    Return PK range and row count for a customer's slice of a table.

    Used by the chunk planner to determine how many chunks are needed and
    how wide each chunk's PK window should be.

    Args:
        conn:          Open source connection.
        database:      Source database name.
        table:         Table name.
        pk_col:        Primary-key column name.
        tenant_column: Column used as the tenant discriminator.
        customer_id:   Tenant value to measure.

    Returns:
        Tuple (min_pk, max_pk, total_rows) or None if the slice is empty.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MIN(`{pk_col}`) AS mn, MAX(`{pk_col}`) AS mx, COUNT(*) AS cnt "
            f"FROM `{database}`.`{table}` "
            f"WHERE `{tenant_column}` = %s",
            (customer_id,),
        )
        row = cur.fetchone()

    if not row or row["cnt"] == 0 or row["mn"] is None:
        return None

    return int(row["mn"]), int(row["mx"]), int(row["cnt"])


# ── Chunk planning ────────────────────────────────────────────────────────────

def _compute_chunks(
    min_pk: int,
    max_pk: int,
    total_rows: int,
    chunk_size: int,
) -> list[tuple[int, int]]:
    """
    Divide a PK range into chunks targeting approximately chunk_size rows each.

    Accounts for PK density so that sparse auto-increment tables (where many
    PK values were deleted) still produce chunks of roughly the right row count.

    Args:
        min_pk:     Smallest PK value in the customer slice.
        max_pk:     Largest PK value in the customer slice.
        total_rows: Actual row count in [min_pk, max_pk] for the customer.
        chunk_size: Desired number of rows per chunk.

    Returns:
        List of (start_pk, end_pk) inclusive ranges covering [min_pk, max_pk].
    """
    pk_range = max_pk - min_pk + 1
    density  = total_rows / pk_range if pk_range > 0 else 1.0

    # Widen the PK step for sparse tables so each chunk still hits ~chunk_size rows
    step = max(1, math.ceil(chunk_size / density))

    chunks: list[tuple[int, int]] = []
    start = min_pk
    while start <= max_pk:
        end = min(start + step - 1, max_pk)
        chunks.append((start, end))
        start = end + 1

    return chunks


# ── Bash script emission ──────────────────────────────────────────────────────

def _emit_bash(
    out_path: str,
    config: dict,
    customer_id: str,
    tenant_column: str,
    all_chunks: list[dict],
) -> None:
    """
    Write a self-contained bash migration script to out_path.

    The generated script:
    - Source mysql/mysqldump always run via docker exec (uses container's 5.7 client).
    - Target supports two modes driven by config["target"].get("container"):
        docker  — target is a local Docker container (default, local POC).
        direct  — no container field; connects to Aurora/remote MySQL directly.
    - Credentials managed via --defaults-file at mode 0600; docker-cp'd into the
      source container always, and into the target container only in docker mode.
    - Captures a MySQL binlog coordinate in the first chunk via --master-data=2.
    - Runs remaining chunks in parallel via xargs -P $PARALLELISM.
    - Uses .done marker files to skip completed chunks on re-run.
    - Uses REPLACE INTO (--replace) so re-running a chunk is always safe.

    Args:
        out_path:      Destination file path for the generated script.
        config:        Loaded config dict with source, target, state_dir, etc.
        customer_id:   Tenant value whose data is being migrated.
        tenant_column: Column used as the tenant discriminator.
        all_chunks:    Ordered list of chunk dicts (table, pk_col, start, end).
    """
    src = config["source"]
    tgt = config["target"]
    src_container = src.get("container", "marketo-src")
    tgt_container = tgt.get("container")           # None → Aurora/direct mode
    tgt_host      = tgt.get("host", "127.0.0.1")
    tgt_port      = int(tgt.get("port", 3306))
    tgt_mode      = "docker" if tgt_container else "direct"
    state_dir     = config["state_dir"]
    work_dir      = config["work_dir"]
    parallelism   = config.get("parallelism", 4)

    # Accumulate lines of the generated script in this list
    L: list[str] = []

    def w(*lines: str) -> None:
        """Append one or more lines to the output buffer."""
        L.extend(lines)

    # ── Script header ────────────────────────────────────────────────────────
    w(
        "#!/usr/bin/env bash",
        "# Generated by marketo_migration.orchestrator — do not edit by hand.",
        f"# Target mode: {tgt_mode} ({'docker exec into container' if tgt_mode == 'docker' else 'direct Aurora/MySQL connection'}).",
        "set -Eeuo pipefail",
        "",
        "# ── Embedded configuration ───────────────────────────────────────────",
        f'SRC_CONTAINER="{src_container}"',
        f'SRC_USER="{src["user"]}"',
        f'SRC_DB="{src["database"]}"',
    )
    # Emit target connection variables — docker mode uses container name,
    # direct/Aurora mode uses host and port for a TCP connection.
    if tgt_mode == "docker":
        w(f'TGT_CONTAINER="{tgt_container}"')
    else:
        w(f'TGT_HOST="{tgt_host}"', f'TGT_PORT="{tgt_port}"')
    w(
        f'TGT_USER="{tgt["user"]}"',
        f'TGT_DB="{tgt["database"]}"',
        f'STATE_DIR="{state_dir}"',
        f'WORK_DIR="{work_dir}"',
        f'CUSTOMER_ID="{customer_id}"',
        f'TENANT_COL="{tenant_column}"',
        f'PARALLELISM="{parallelism}"',
        "",
        "# ── Credentials (read from env — never embedded in script) ───────────",
        ': "${SRC_PASSWORD:?SRC_PASSWORD env var is required}"',
        ': "${TGT_PASSWORD:?TGT_PASSWORD env var is required}"',
    )

    # ── Defaults-file setup ──────────────────────────────────────────────────
    w(
        "",
        "# ── Temporary defaults files (host-side, mode 0600) ─────────────────",
        "# Passwords are written to temp files and docker cp'd into containers",
        "# so they never appear on any command line or in logs.",
        'SRC_DEFAULTS_HOST=$(mktemp)',
        'TGT_DEFAULTS_HOST=$(mktemp)',
        'chmod 0600 "$SRC_DEFAULTS_HOST" "$TGT_DEFAULTS_HOST"',
        'printf "[client]\\npassword=%s\\n" "$SRC_PASSWORD" > "$SRC_DEFAULTS_HOST"',
        'printf "[client]\\npassword=%s\\n" "$TGT_PASSWORD" > "$TGT_DEFAULTS_HOST"',
        "",
        "# Source container-side path is unique per shell PID to avoid collisions",
        '# when multiple migrations run in parallel.',
        'SRC_DEFAULTS_CTR="/tmp/.mig_src_$$.cnf"',
        'docker cp "$SRC_DEFAULTS_HOST" "${SRC_CONTAINER}:${SRC_DEFAULTS_CTR}"',
        'docker exec "$SRC_CONTAINER" chmod 0600 "$SRC_DEFAULTS_CTR"',
    )
    # In docker mode, also copy the target credentials into the target container.
    # In direct/Aurora mode the host-side TGT_DEFAULTS_HOST is used directly.
    if tgt_mode == "docker":
        w(
            'TGT_DEFAULTS_CTR="/tmp/.mig_tgt_$$.cnf"',
            'docker cp "$TGT_DEFAULTS_HOST" "${TGT_CONTAINER}:${TGT_DEFAULTS_CTR}"',
            'docker exec "$TGT_CONTAINER" chmod 0600 "$TGT_DEFAULTS_CTR"',
        )

    # ── Cleanup trap ─────────────────────────────────────────────────────────
    w(
        "",
        "# Remove host and container credential files on any exit path",
        "_cleanup() {",
        '    rm -f "$SRC_DEFAULTS_HOST" "$TGT_DEFAULTS_HOST"',
        '    docker exec "$SRC_CONTAINER" rm -f "$SRC_DEFAULTS_CTR" 2>/dev/null || true',
    )
    # In docker mode, also clean up the credentials inside the target container
    if tgt_mode == "docker":
        w('    docker exec "$TGT_CONTAINER" rm -f "$TGT_DEFAULTS_CTR" 2>/dev/null || true')
    w(
        "}",
        'trap _cleanup EXIT',
    )

    # ── Logging functions ────────────────────────────────────────────────────
    w(
        "",
        "# ── Logging functions ────────────────────────────────────────────────",
        "",
        "# Print an informational message with ISO-8601 timestamp to stdout.",
        "log_info() {",
        "    local timestamp",
        "    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')",
        '    echo "[${timestamp}] [INFO]  $*"',
        "}",
        "",
        "# Print a warning message with timestamp to stderr (non-fatal).",
        "log_warn() {",
        "    local timestamp",
        "    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')",
        '    echo "[${timestamp}] [WARN]  $*" >&2',
        "}",
        "",
        "# Print an error message with timestamp to stderr.",
        "log_error() {",
        "    local timestamp",
        "    timestamp=$(date '+%Y-%m-%dT%H:%M:%S')",
        '    echo "[${timestamp}] [ERROR] $*" >&2',
        "}",
    )

    # ── tgt_mysql helper ─────────────────────────────────────────────────────
    # Generate a tgt_mysql() function that abstracts the target connection so
    # run_chunk does not need to know whether the target is Docker or Aurora.
    w(
        "",
        "# ── Target connection helper ─────────────────────────────────────────",
        "",
        "# Execute a mysql command against the target database.",
        "# Accepts all mysql CLI arguments and passes them through unchanged.",
        "# In docker mode: runs mysql inside the target container via docker exec.",
        "# In direct mode: connects directly to Aurora/remote MySQL via TCP.",
        "tgt_mysql() {",
    )
    if tgt_mode == "docker":
        w(
            '    docker exec -i "$TGT_CONTAINER" mysql \\',
            '        --defaults-file="${TGT_DEFAULTS_CTR}" \\',
            '        -u "${TGT_USER}" \\',
            '        "$@"',
        )
    else:
        w(
            '    mysql \\',
            '        --defaults-file="${TGT_DEFAULTS_HOST}" \\',
            '        -h "${TGT_HOST}" \\',
            '        -P "${TGT_PORT}" \\',
            '        -u "${TGT_USER}" \\',
            '        "$@"',
        )
    w("}")

    # ── Logging + state dir setup ────────────────────────────────────────────
    w(
        "",
        "# ── Log file (tee stdout+stderr into a timestamped log file) ─────────",
        'mkdir -p "$STATE_DIR/coords" "$WORK_DIR"',
        'LOG_FILE="$STATE_DIR/migration_$(date +%Y%m%d_%H%M%S).log"',
        'exec > >(tee -a "$LOG_FILE") 2>&1',
    )

    # ── run_chunk function ───────────────────────────────────────────────────
    w(
        "",
        "# ── run_chunk ────────────────────────────────────────────────────────",
        "",
        "# Migrate one PK-range chunk for the configured customer.",
        "# Steps:",
        "#   1. Skip if a .done marker already exists (resumability).",
        "#   2. Dump the customer slice from source via mysqldump (docker exec).",
        "#   3. Capture the binlog coordinate from the dump (first chunk only).",
        "#   4. Pipe the dump into the target via mysql (docker exec).",
        "#   5. Verify row counts match between source and target.",
        "#   6. Write the .done marker and remove the temp dump file.",
        "#",
        "# Args: table  pk_col  start  end  is_first(0|1)",
        "run_chunk() {",
        "    local table=$1",
        "    local pk_col=$2",
        "    local start=$3",
        "    local end=$4",
        "    local is_first=${5:-0}",
        "",
        '    local marker="${STATE_DIR}/${table}__${start}_${end}.done"',
        '    local dump_file="${WORK_DIR}/${table}__${start}_${end}.sql"',
        "",
        "    # Skip this chunk if it was already completed in a previous run.",
        "    # This is the key resumability mechanism: the script can be killed",
        "    # and restarted without re-transmitting data already on the target.",
        '    if [[ -f "${marker}" ]]; then',
        '        log_info "SKIP ${table} [${start},${end}] — .done marker exists"',
        "        return 0",
        "    fi",
        "",
        "    # Determine the --master-data flag.",
        "    # Only the first chunk uses =2 to embed a binlog coordinate comment.",
        "    # Subsequent chunks use =0 to avoid the unnecessary FLUSH LOGS.",
        '    local md_flag="--master-data=0"',
        '    if [[ "${is_first}" == "1" && ! -f "${STATE_DIR}/coords/binlog.coord" ]]; then',
        '        md_flag="--master-data=2"',
        "    fi",
        "",
        '    log_info "DUMP ${table} [${start},${end}] md_flag=${md_flag}"',
        "",
        "    # Run mysqldump inside the source container using the container's",
        "    # own MySQL 5.7 client to avoid host client version mismatches.",
        "    # --replace generates REPLACE INTO so re-runs are idempotent.",
        '    docker exec "$SRC_CONTAINER" mysqldump \\',
        '        --defaults-file="${SRC_DEFAULTS_CTR}" \\',
        '        -u "${SRC_USER}" \\',
        "        --single-transaction \\",
        "        --skip-lock-tables \\",
        "        --no-tablespaces \\",
        "        --no-create-info \\",
        "        --skip-add-drop-table \\",
        "        --skip-add-locks \\",
        "        --skip-disable-keys \\",
        "        --replace \\",
        "        --extended-insert \\",
        "        --hex-blob \\",
        "        --set-gtid-purged=OFF \\",
        '        "${md_flag}" \\',
        '        --where="${TENANT_COL}=\'${CUSTOMER_ID}\' AND ${pk_col} BETWEEN ${start} AND ${end}" \\',
        '        "${SRC_DB}" "${table}" > "${dump_file}"',
        "",
        "    # Extract the binlog coordinate from the dump comment and write it",
        "    # to coords/binlog.coord so CDC can use it as its start position.",
        '    if [[ "${md_flag}" == "--master-data=2" ]]; then',
        '        grep "^-- CHANGE MASTER TO" "${dump_file}" > "${STATE_DIR}/coords/binlog.coord"',
        '        log_info "Binlog coord captured: $(cat "${STATE_DIR}/coords/binlog.coord")"',
        "    fi",
        "",
        "    # Prepend session settings to disable FK checks and binlog on the",
        "    # target, then pipe the dump into the target via tgt_mysql().",
        "    # SET SESSION sql_log_bin=0 requires SUPER (docker) or rds_superuser (Aurora).",
        "    {",
        '        printf "SET SESSION FOREIGN_KEY_CHECKS=0;\\n"',
        '        printf "SET SESSION UNIQUE_CHECKS=0;\\n"',
        '        printf "SET SESSION sql_log_bin=0;\\n"',
        '        cat "${dump_file}"',
        '    } | tgt_mysql "${TGT_DB}"',
        "",
        "    # Parity check: count rows on both sides for this chunk's PK range.",
        "    # A mismatch means the load was incomplete; the chunk will not be",
        "    # marked done and will be retried on the next run.",
        "    local src_count",
        "    local tgt_count",
        '    src_count=$(docker exec "$SRC_CONTAINER" mysql \\',
        '        --defaults-file="${SRC_DEFAULTS_CTR}" -u "${SRC_USER}" -sN "${SRC_DB}" \\',
        "        -e \"SELECT COUNT(*) FROM ${table} WHERE ${TENANT_COL}='${CUSTOMER_ID}' AND ${pk_col} BETWEEN ${start} AND ${end}\")",
        '    tgt_count=$(tgt_mysql -sN "${TGT_DB}" \\',
        "        -e \"SELECT COUNT(*) FROM ${table} WHERE ${TENANT_COL}='${CUSTOMER_ID}' AND ${pk_col} BETWEEN ${start} AND ${end}\")",
        "",
        '    if [[ "${src_count}" != "${tgt_count}" ]]; then',
        '        log_error "FAIL parity ${table} [${start},${end}]: src=${src_count} tgt=${tgt_count}"',
        '        rm -f "${dump_file}"',
        "        return 1",
        "    fi",
        "",
        "    # Parity passed: clean up the dump file and create the .done marker",
        "    # so this chunk is skipped on any subsequent run.",
        '    rm -f "${dump_file}"',
        '    touch "${marker}"',
        '    log_info "DONE ${table} [${start},${end}] rows=${src_count}"',
        "}",
        "",
        "# Export run_chunk, tgt_mysql, and helpers so xargs subshells can call them",
        "export -f run_chunk tgt_mysql log_info log_warn log_error",
    )
    # Export the correct set of TGT variables depending on the connection mode
    if tgt_mode == "docker":
        w(
            "export SRC_DEFAULTS_CTR TGT_DEFAULTS_CTR SRC_CONTAINER TGT_CONTAINER \\",
            "       SRC_USER SRC_DB TGT_USER TGT_DB \\",
            "       STATE_DIR WORK_DIR CUSTOMER_ID TENANT_COL LOG_FILE",
        )
    else:
        w(
            "export SRC_DEFAULTS_CTR TGT_DEFAULTS_HOST SRC_CONTAINER \\",
            "       TGT_HOST TGT_PORT TGT_USER TGT_DB \\",
            "       STATE_DIR WORK_DIR CUSTOMER_ID TENANT_COL LOG_FILE",
        )
    w(
        "",
        "# ── Execute chunks ────────────────────────────────────────────────────",
    )

    # ── Chunk execution block ────────────────────────────────────────────────
    if not all_chunks:
        w(
            'log_info "No chunkable tables found for customer ${CUSTOMER_ID}. Exiting."',
            "exit 0",
        )
    else:
        first = all_chunks[0]
        rest  = all_chunks[1:]

        w(
            f'log_info "Migration start: customer={customer_id} chunks={len(all_chunks)}"',
            "",
            "# First chunk runs single-threaded to capture the binlog coordinate",
            "# cleanly before any parallel activity starts.",
            f'run_chunk "{first["table"]}" "{first["pk_col"]}" '
            f'{first["start"]} {first["end"]} 1',
        )

        if rest:
            w(
                "",
                "# Remaining chunks run in parallel up to $PARALLELISM workers.",
                "# xargs reads one chunk command per line and runs them concurrently.",
                "_CHUNKS_TMP=$(mktemp)",
                'trap "rm -f $_CHUNKS_TMP; _cleanup" EXIT',
                "",
                "cat > \"$_CHUNKS_TMP\" << 'CHUNKS_EOF'",
            )
            for chunk in rest:
                w(
                    f'run_chunk "{chunk["table"]}" "{chunk["pk_col"]}" '
                    f'{chunk["start"]} {chunk["end"]} 0'
                )
            w(
                "CHUNKS_EOF",
                "",
                'xargs -P "$PARALLELISM" -I \'{}\' bash -c \'{}\' < "$_CHUNKS_TMP"',
                'rm -f "$_CHUNKS_TMP"',
            )

        # Print the CDC hand-off coordinate for the operator
        w(
            "",
            'log_info "All chunks complete."',
            'log_info "CDC hand-off point:"',
            'if [[ -f "${STATE_DIR}/coords/binlog.coord" ]]; then',
            '    cat "${STATE_DIR}/coords/binlog.coord"',
            "else",
            '    log_warn "binlog.coord not found — was --master-data=2 needed?"',
            "fi",
        )

    script = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(script)
    os.chmod(out_path, 0o755)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """
    CLI entry point for the orchestrator.

    Connects to the source database, plans the migration (table discovery,
    PK inspection, chunk computation), writes the plan JSON, and emits
    the migration bash script.
    """
    args   = _parse_args()
    config = _load_config(args.config)

    src           = config["source"]
    database      = src["database"]
    include_tables = config.get("tables") or []
    exclude_tables = config.get("exclude_tables") or []
    chunk_size    = config.get("chunk_size", 5000)

    src_conn = _connect(src, "SRC_PASSWORD")

    tables = _tables_with_tenant_col(
        src_conn, database, args.tenant_column, include_tables, exclude_tables
    )

    plan: dict = {
        "customer_id":     args.customer_id,
        "tenant_column":   args.tenant_column,
        "source_database": database,
        "tables":          {},
        "skipped":         [],
        "all_chunks":      [],
    }

    all_chunks: list[dict]           = []
    summary:    list[tuple[str, int, int]] = []

    log_info(f"Planning migration for customer: {args.customer_id}")
    log_info(f"Candidate tables: {tables}")

    for table in tables:
        pk_col, chunkable, reason = _pk_info(src_conn, database, table)

        if not chunkable:
            # Record the reason so verify.py can exclude the table
            entry = {"table": table, "reason": reason}
            plan["skipped"].append(entry)
            log_info(f"  SKIP {table:<30s} reason={reason}")
            continue

        stats = _table_stats(
            src_conn,
            database,
            table,
            pk_col,       # type: ignore[arg-type]
            args.tenant_column,
            args.customer_id,
        )

        if stats is None:
            entry = {"table": table, "reason": "no rows for customer"}
            plan["skipped"].append(entry)
            log_info(f"  SKIP {table:<30s} reason=no rows for {args.customer_id}")
            continue

        min_pk, max_pk, total_rows = stats
        chunks = _compute_chunks(min_pk, max_pk, total_rows, chunk_size)

        plan["tables"][table] = {
            "pk_col":      pk_col,
            "min_pk":      min_pk,
            "max_pk":      max_pk,
            "total_rows":  total_rows,
            "chunk_count": len(chunks),
            "chunks":      [{"start": s, "end": e} for s, e in chunks],
        }

        for start, end in chunks:
            all_chunks.append(
                {"table": table, "pk_col": pk_col, "start": start, "end": end}
            )

        summary.append((table, total_rows, len(chunks)))
        log_info(f"  PLAN {table:<30s} rows={total_rows:>8,}  chunks={len(chunks):>4}")

    src_conn.close()

    plan["all_chunks"] = all_chunks

    # Write the plan JSON so verify.py knows which tables were skipped
    with open(args.plan_json, "w") as f:
        json.dump(plan, f, indent=2)

    # Emit the self-contained bash migration script
    _emit_bash(args.out, config, args.customer_id, args.tenant_column, all_chunks)

    log_info("Table summary:")
    for table, rows, chunks in summary:
        log_info(f"  {table}: {rows:,} rows → {chunks} chunks")

    if plan["skipped"]:
        log_info(f"Skipped ({len(plan['skipped'])}):")
        for s in plan["skipped"]:
            log_info(f"  {s['table']}: {s['reason']}")

    log_info(f"Total chunks : {len(all_chunks)}")
    log_info(f"Script       : {args.out}")
    log_info(f"Plan JSON    : {args.plan_json}")


if __name__ == "__main__":
    main()
