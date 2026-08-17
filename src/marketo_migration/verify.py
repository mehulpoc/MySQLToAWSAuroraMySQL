"""
Verification tool: row-count and CRC32 checksum parity per table per customer.

Usage:
    python -m marketo_migration.verify \\
        --config config.json \\
        --customer-id acc_12345 \\
        --tenant-column account_id \\
        --plan-json plan.json   # optional; excludes orchestrator-skipped tables

Exit 0 if everything matches, 1 if any divergence.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

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
    Parse command-line arguments for the verify tool.

    Returns the populated namespace with config, customer_id, tenant_column,
    and the optional plan_json path.
    """
    p = argparse.ArgumentParser(
        description="Verify row-count and CRC32 parity between source and target."
    )
    p.add_argument("--config",         required=True,  help="Path to config JSON")
    p.add_argument("--customer-id",    required=True,  help="Tenant identifier to verify")
    p.add_argument("--tenant-column",  required=True,  help="Column that holds the tenant ID")
    p.add_argument(
        "--plan-json",
        default=None,
        help=(
            "Optional plan JSON produced by the orchestrator.  Tables listed as "
            "skipped (e.g. composite PK) are excluded from verification."
        ),
    )
    return p.parse_args()


# ── Database connection ───────────────────────────────────────────────────────

def _connect(endpoint: dict, password_env: str) -> pymysql.Connection:
    """
    Open a PyMySQL connection to the given endpoint.

    The password is read from the environment variable named by password_env
    so that it is never embedded in config files or command lines.

    Args:
        endpoint:     Dict with keys host, port, user, database.
        password_env: Name of the environment variable that holds the password.

    Returns:
        An open pymysql.Connection using DictCursor.
    """
    return pymysql.connect(
        host=endpoint["host"],
        port=int(endpoint["port"]),
        user=endpoint["user"],
        password=os.environ.get(password_env, ""),
        database=endpoint["database"],
        cursorclass=pymysql.cursors.DictCursor,
    )


# ── Schema helpers ────────────────────────────────────────────────────────────

def _tables_with_tenant_col(
    conn: pymysql.Connection,
    database: str,
    tenant_column: str,
) -> list[str]:
    """
    Return the names of all BASE TABLEs in database that contain tenant_column.

    Queries INFORMATION_SCHEMA so no privileged access is required beyond
    SELECT on the target database.

    Args:
        conn:          Open source or target connection.
        database:      Database name to inspect.
        tenant_column: Column name used as the tenant discriminator.

    Returns:
        Sorted list of table names that carry the tenant column.
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
        return [r["TABLE_NAME"] for r in cur.fetchall()]


def _column_list(
    conn: pymysql.Connection,
    database: str,
    table: str,
) -> list[str]:
    """
    Return the ordered column names for table in database.

    Column order must be consistent between source and target for the
    CONCAT_WS checksum to produce matching results.

    Args:
        conn:     Open connection to the database being inspected.
        database: Database name.
        table:    Table name.

    Returns:
        List of column names in ORDINAL_POSITION order.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME   = %s
            ORDER BY ORDINAL_POSITION
            """,
            (database, table),
        )
        return [r["COLUMN_NAME"] for r in cur.fetchall()]


# ── Metric functions ──────────────────────────────────────────────────────────

def _row_count(
    conn: pymysql.Connection,
    table: str,
    tenant_column: str,
    customer_id: str,
) -> int:
    """
    Count rows belonging to customer_id in table.

    Args:
        conn:          Open connection to the database being measured.
        table:         Table name (back-tick quoted in the query).
        tenant_column: Column used as the tenant discriminator.
        customer_id:   Tenant value to filter by.

    Returns:
        Integer row count for the customer slice.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE `{tenant_column}` = %s",
            (customer_id,),
        )
        return int(cur.fetchone()["cnt"])


def _checksum(
    conn: pymysql.Connection,
    table: str,
    cols: list[str],
    tenant_column: str,
    customer_id: str,
) -> int:
    """
    Compute a row-order-independent checksum for a customer's slice of a table.

    Uses BIT_XOR(CRC32(CONCAT_WS(...))) which is commutative and associative,
    so the result is independent of the order rows are returned by the engine.

    NULLs are stringified as the literal 'NULL' to distinguish them from empty
    strings — both would otherwise produce an identical CRC32 hash.

    Args:
        conn:          Open connection to the database being checksummed.
        table:         Table name.
        cols:          Ordered list of column names to include in the hash.
        tenant_column: Column used as the tenant discriminator.
        customer_id:   Tenant value to filter by.

    Returns:
        Unsigned integer checksum (0 for an empty slice).
    """
    # Build per-column expressions that normalise NULLs to the string 'NULL'
    col_exprs = ", ".join(
        f"IFNULL(CAST(`{c}` AS CHAR), 'NULL')" for c in cols
    )
    sql = (
        f"SELECT IFNULL("
        f"BIT_XOR(CAST(CRC32(CONCAT_WS('|', {col_exprs})) AS UNSIGNED)), 0) AS chk "
        f"FROM `{table}` WHERE `{tenant_column}` = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (customer_id,))
        row = cur.fetchone()
    return int(row["chk"])


# ── Core verification logic ───────────────────────────────────────────────────

def verify(
    config: dict,
    customer_id: str,
    tenant_column: str,
    skipped_tables: set[str] | None = None,
) -> dict:
    """
    Run row-count and CRC32 parity checks for every applicable table.

    Connects to both source and target, discovers tables that carry
    tenant_column, optionally excludes orchestrator-skipped tables, then
    compares COUNT(*) and BIT_XOR(CRC32) results for each table.

    Args:
        config:         Loaded config dict with source and target sub-dicts.
        customer_id:    Tenant value to verify (e.g. 'acc_12345').
        tenant_column:  Column used as the tenant discriminator.
        skipped_tables: Optional set of table names to exclude (e.g. composite
                        PK tables that were never migrated).

    Returns:
        Report dict with keys: customer_id, tables (per-table results), ok (bool).
    """
    src_ep = config["source"]
    tgt_ep = config["target"]

    src_conn = _connect(src_ep, "SRC_PASSWORD")
    tgt_conn = _connect(tgt_ep, "TGT_PASSWORD")

    # Discover tables that carry the tenant column on the source side
    tables = _tables_with_tenant_col(src_conn, src_ep["database"], tenant_column)

    # Exclude tables that the orchestrator could not migrate (composite PK etc.)
    if skipped_tables:
        skipped = skipped_tables & set(tables)
        if skipped:
            log_info(f"  [SKIP] orchestrator skipped (not verified): {', '.join(sorted(skipped))}")
        tables = [t for t in tables if t not in skipped_tables]

    report: dict = {"customer_id": customer_id, "tables": {}, "ok": True}

    for table in tables:
        # Row count comparison
        src_count = _row_count(src_conn, table, tenant_column, customer_id)
        tgt_count = _row_count(tgt_conn, table, tenant_column, customer_id)
        count_ok  = src_count == tgt_count

        # CRC32 checksum comparison — uses source column list for both sides
        # so column order is guaranteed to be identical
        cols    = _column_list(src_conn, src_ep["database"], table)
        src_crc = _checksum(src_conn, table, cols, tenant_column, customer_id)
        tgt_crc = _checksum(tgt_conn, table, cols, tenant_column, customer_id)
        crc_ok  = src_crc == tgt_crc

        table_ok = count_ok and crc_ok
        if not table_ok:
            report["ok"] = False

        report["tables"][table] = {
            "src_count":    src_count,
            "tgt_count":    tgt_count,
            "count_ok":     count_ok,
            "src_checksum": src_crc,
            "tgt_checksum": tgt_crc,
            "checksum_ok":  crc_ok,
            "ok":           table_ok,
        }

        status = "OK" if table_ok else "FAIL"
        log_info(
            f"  [{status}] {table}: "
            f"src_rows={src_count} tgt_rows={tgt_count} "
            f"src_crc={src_crc} tgt_crc={tgt_crc}"
        )

    src_conn.close()
    tgt_conn.close()

    return report


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """
    CLI entry point for the verify tool.

    Parses arguments, loads the config, optionally reads the plan JSON to
    determine which tables were skipped by the orchestrator, runs the parity
    check, prints a JSON report, and exits 0 (PASS) or 1 (FAIL).
    """
    args = _parse_args()

    with open(args.config) as f:
        config = json.load(f)

    # Load the list of orchestrator-skipped tables if a plan JSON was provided
    skipped_tables: set[str] | None = None
    if args.plan_json:
        with open(args.plan_json) as f:
            plan = json.load(f)
        skipped_tables = {s["table"] for s in plan.get("skipped", [])}

    log_info(f"Verifying customer: {args.customer_id}")
    report = verify(config, args.customer_id, args.tenant_column, skipped_tables)

    log_info(json.dumps(report, indent=2))

    if report["ok"]:
        log_info("Result: PASS")
        sys.exit(0)
    else:
        log_error("Result: FAIL — see report above")


if __name__ == "__main__":
    main()
