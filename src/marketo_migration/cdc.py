"""
CDC streamer: reads binlog from source, filters by tenant, applies idempotent
DML to the target.

Usage:
    python -m marketo_migration.cdc \\
        --config config.json \\
        --customer-id acc_12345 \\
        --tenant-column account_id \\
        --state-dir /tmp/marketo_migration/state \\
        --batch-size 500 \\
        --commit-interval-s 2.0
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

import pymysql
import pymysql.cursors
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.row_event import (
    DeleteRowsEvent,
    UpdateRowsEvent,
    WriteRowsEvent,
)


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


# ── Constants ─────────────────────────────────────────────────────────────────

CHECKPOINT_FNAME   = "cdc.checkpoint.json"
BINLOG_COORD_FNAME = "binlog.coord"


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments for the CDC streamer.

    Returns the populated namespace with all runtime parameters.
    """
    p = argparse.ArgumentParser(
        description="Stream MySQL binlog changes from source to target for one tenant."
    )
    p.add_argument("--config",            required=True,  help="Path to config JSON")
    p.add_argument("--customer-id",       required=True,  help="Tenant identifier to stream")
    p.add_argument("--tenant-column",     required=True,  help="Column holding the tenant ID")
    p.add_argument("--state-dir",         required=True,  help="Directory for checkpoint files")
    p.add_argument("--batch-size",        type=int,   default=500,  help="Max DML ops per commit")
    p.add_argument("--commit-interval-s", type=float, default=2.0,  help="Max seconds between commits")
    return p.parse_args()


# ── Checkpoint I/O ────────────────────────────────────────────────────────────

def _load_checkpoint(state_dir: str) -> Optional[tuple[str, int]]:
    """
    Load the binlog start position from the best available source.

    Preference order:
      1. state_dir/coords/cdc.checkpoint.json  — written after each commit;
         used on CDC restart to resume exactly where it left off.
      2. state_dir/coords/binlog.coord          — written by the historical
         migration's first chunk (--master-data=2); used for initial startup.

    Args:
        state_dir: Root state directory shared with the migration script.

    Returns:
        Tuple (log_file, log_pos) on success, None if neither file exists.
    """
    coords_dir      = os.path.join(state_dir, "coords")
    checkpoint_path = os.path.join(coords_dir, CHECKPOINT_FNAME)
    binlog_path     = os.path.join(coords_dir, BINLOG_COORD_FNAME)

    if os.path.exists(checkpoint_path):
        with open(checkpoint_path) as f:
            data = json.load(f)
        log_info(f"Resuming from CDC checkpoint: {data['log_file']} pos={data['log_pos']}")
        return data["log_file"], int(data["log_pos"])

    if os.path.exists(binlog_path):
        # Fall back to the hand-off coordinate from the historical migration
        return _parse_binlog_coord(binlog_path)

    return None


def _parse_binlog_coord(path: str) -> Optional[tuple[str, int]]:
    """
    Parse a MySQL CHANGE MASTER TO comment written by mysqldump --master-data=2.

    Expected line format (the comment produced by mysqldump):
        -- CHANGE MASTER TO MASTER_LOG_FILE='mysql-bin.000001', MASTER_LOG_POS=1234;

    Args:
        path: Absolute path to the binlog.coord file.

    Returns:
        Tuple (log_file, log_pos) on success, None if the file cannot be parsed.
    """
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "CHANGE MASTER TO" not in line and "CHANGE REPLICATION SOURCE TO" not in line:
                continue

            # Extract FILE and POS tokens from the comma-separated list
            file_val: Optional[str] = None
            pos_val:  Optional[int] = None

            for token in line.replace(";", "").split(","):
                token = token.strip()
                if "LOG_FILE" in token:
                    file_val = token.split("=", 1)[1].strip().strip("'")
                elif "LOG_POS" in token:
                    pos_val = int(token.split("=", 1)[1].strip())

            if file_val and pos_val is not None:
                log_info(f"Using binlog.coord hand-off: {file_val} pos={pos_val}")
                return file_val, pos_val

    log_error(f"Could not parse binlog.coord at {path}")
    return None


def _save_checkpoint(state_dir: str, log_file: str, log_pos: int) -> None:
    """
    Atomically persist the current binlog position to the checkpoint file.

    Writes to a .tmp file first then renames it via os.replace(), which is
    atomic on POSIX filesystems.  This prevents a torn checkpoint if the
    process is killed mid-write.

    Args:
        state_dir: Root state directory.
        log_file:  Current binlog file name (e.g. 'mysql-bin.000003').
        log_pos:   Current byte offset within the binlog file.
    """
    coords_dir = os.path.join(state_dir, "coords")
    os.makedirs(coords_dir, exist_ok=True)

    target = os.path.join(coords_dir, CHECKPOINT_FNAME)
    tmp    = target + ".tmp"

    data = {
        "log_file": log_file,
        "log_pos":  log_pos,
        "last_ts":  time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Write to a temp file; os.replace provides the atomic rename
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, target)

    _logger.debug("Checkpoint saved: %s pos=%d", log_file, log_pos)


# ── Schema cache ──────────────────────────────────────────────────────────────

class _SchemaCache:
    """
    Lazily caches the primary-key column name and ordered column list per table.

    Avoids repeated INFORMATION_SCHEMA queries during high-throughput streaming
    by looking up each table only once and storing the result in memory.
    """

    def __init__(self, conn: pymysql.Connection, database: str) -> None:
        """
        Initialise the cache with an open source connection and database name.

        Args:
            conn:     Open PyMySQL connection to the source database.
            database: Source database name used in INFORMATION_SCHEMA queries.
        """
        self._conn = conn
        self._db   = database
        self._pk:   dict[str, str]        = {}
        self._cols: dict[str, list[str]]  = {}

    def pk_col(self, table: str) -> str:
        """
        Return the single-column primary-key name for table, fetching if needed.

        Args:
            table: Table name to look up.

        Returns:
            Column name of the first (and only) primary-key column.

        Raises:
            RuntimeError: If the table has no single-column integer primary key.
        """
        if table not in self._pk:
            self._pk[table] = self._fetch_pk(table)
        return self._pk[table]

    def columns(self, table: str) -> list[str]:
        """
        Return the ordered column list for table, fetching from schema if needed.

        Args:
            table: Table name to look up.

        Returns:
            List of column names in ORDINAL_POSITION order.
        """
        if table not in self._cols:
            self._cols[table] = self._fetch_cols(table)
        return self._cols[table]

    def _fetch_pk(self, table: str) -> str:
        """
        Query INFORMATION_SCHEMA for the first column of the primary key.

        Args:
            table: Table name.

        Returns:
            Primary-key column name.

        Raises:
            RuntimeError: If no primary key is found.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA    = %s
                  AND TABLE_NAME      = %s
                  AND CONSTRAINT_NAME = 'PRIMARY'
                ORDER BY ORDINAL_POSITION
                LIMIT 1
                """,
                (self._db, table),
            )
            row = cur.fetchone()

        if not row:
            raise RuntimeError(f"No PK found for table {self._db}.{table}")

        return row["COLUMN_NAME"]

    def _fetch_cols(self, table: str) -> list[str]:
        """
        Query INFORMATION_SCHEMA for all columns of table in ordinal order.

        Args:
            table: Table name.

        Returns:
            Ordered list of column names.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME   = %s
                ORDER BY ORDINAL_POSITION
                """,
                (self._db, table),
            )
            return [r["COLUMN_NAME"] for r in cur.fetchall()]


# ── DML builders ──────────────────────────────────────────────────────────────

def _build_upsert(
    table: str,
    values: dict,
    cols: list[str],
) -> tuple[str, tuple]:
    """
    Build an idempotent INSERT ... ON DUPLICATE KEY UPDATE statement.

    Used for both WriteRowsEvent (new insert) and UpdateRowsEvent (column
    update) so that re-applying the same event is always safe.

    Args:
        table:  Target table name.
        values: Row values dict (column name → value) from the binlog event.
        cols:   Ordered column list used to build placeholders.

    Returns:
        Tuple of (sql_string, args_tuple) ready for cursor.execute().
    """
    col_list     = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join("%s" for _ in cols)
    updates      = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols)

    sql = (
        f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    args = tuple(values.get(c) for c in cols)

    return sql, args


def _build_delete(
    table: str,
    pk_col: str,
    values: dict,
) -> tuple[str, tuple]:
    """
    Build a DELETE statement targeting the primary-key column.

    Args:
        table:   Target table name.
        pk_col:  Primary-key column name.
        values:  Row values dict from the binlog DeleteRowsEvent.

    Returns:
        Tuple of (sql_string, args_tuple) ready for cursor.execute().
    """
    sql = f"DELETE FROM `{table}` WHERE `{pk_col}` = %s"
    return sql, (values[pk_col],)


# ── CDC Streamer ──────────────────────────────────────────────────────────────

class CDCStreamer:
    """
    Connects to a MySQL binlog stream, filters events by tenant, and applies
    the resulting DML to the target database in configurable batches.

    Checkpoints are written atomically after each commit so restarts resume
    from the correct position without replaying already-applied events.
    """

    def __init__(
        self,
        config: dict,
        customer_id: str,
        tenant_column: str,
        state_dir: str,
        batch_size: int,
        commit_interval_s: float,
    ) -> None:
        """
        Initialise the streamer, open the target connection, and set up state.

        Args:
            config:            Loaded config dict with source and target sub-dicts.
            customer_id:       Tenant value to filter events by.
            tenant_column:     Column used as the tenant discriminator.
            state_dir:         Directory for checkpoint and coord files.
            batch_size:        Maximum DML operations to queue before committing.
            commit_interval_s: Maximum seconds between commits regardless of batch size.
        """
        self._config           = config
        self._customer_id      = customer_id
        self._tenant_col       = tenant_column
        self._state_dir        = state_dir
        self._batch_size       = batch_size
        self._commit_interval_s = commit_interval_s
        self._running          = True

        src = config["source"]
        tgt = config["target"]

        # Connection settings passed to BinLogStreamReader (uses raw dict)
        self._src_settings = {
            "host":   src["host"],
            "port":   int(src["port"]),
            "user":   src["user"],
            "passwd": os.environ.get("SRC_PASSWORD", ""),
        }

        # Open a regular PyMySQL connection to the target for DML writes
        self._tgt_conn = pymysql.connect(
            host=tgt["host"],
            port=int(tgt["port"]),
            user=tgt["user"],
            password=os.environ.get("TGT_PASSWORD", ""),
            database=tgt["database"],
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

        # Separate source connection for schema lookups (PK col, column list)
        self._schema_cache = _SchemaCache(
            pymysql.connect(
                host=src["host"],
                port=int(src["port"]),
                user=src["user"],
                password=os.environ.get("SRC_PASSWORD", ""),
                database=src["database"],
                cursorclass=pymysql.cursors.DictCursor,
            ),
            src["database"],
        )

        self._tgt_db = tgt["database"]
        self._src_db = src["database"]

        # Batch state: queued (sql, args) pairs and timing metadata
        self._batch:            list[tuple[str, tuple]] = []
        self._last_commit_ts:   float                   = time.monotonic()
        self._current_log_file: Optional[str]           = None
        self._current_log_pos:  Optional[int]           = None

    def _handle_signal(self, signum: int, frame: object) -> None:
        """
        Handle SIGTERM or SIGINT by requesting a graceful shutdown.

        Sets _running to False so the event loop exits cleanly after the
        current event, then the finally block flushes the pending batch.

        Args:
            signum: Signal number received.
            frame:  Current stack frame (unused).
        """
        log_info(f"Signal {signum} received — draining batch and shutting down.")
        self._running = False

    def _flush(self) -> None:
        """
        Commit all queued DML operations and save a checkpoint.

        Executes every (sql, args) pair in the current batch within a single
        transaction, commits, then atomically writes the latest binlog position
        to the checkpoint file.  Clears the batch after a successful commit.
        """
        if not self._batch:
            return

        with self._tgt_conn.cursor() as cur:
            for sql, args in self._batch:
                cur.execute(sql, args)

        self._tgt_conn.commit()
        self._batch.clear()
        self._last_commit_ts = time.monotonic()

        # Persist the checkpoint so a restart resumes from this position
        if self._current_log_file and self._current_log_pos is not None:
            _save_checkpoint(
                self._state_dir,
                self._current_log_file,
                self._current_log_pos,
            )

        log_info(
            f"Committed batch. Checkpoint: "
            f"{self._current_log_file} pos={self._current_log_pos}"
        )

    def _maybe_flush(self) -> None:
        """
        Flush the batch if the size limit or time interval has been reached.

        Called after each event so that neither the row count nor the wall-clock
        duration between commits exceeds the configured thresholds.
        """
        elapsed = time.monotonic() - self._last_commit_ts
        if len(self._batch) >= self._batch_size or elapsed >= self._commit_interval_s:
            self._flush()

    def _process_event(self, event: object) -> None:
        """
        Translate a binlog row event into DML and append it to the batch.

        Only rows whose tenant column matches the configured customer_id are
        processed; all others are silently ignored.

        Args:
            event: A BinLogStreamReader event (Write, Update, or Delete).
        """
        if isinstance(event, WriteRowsEvent):
            # New rows inserted on source → upsert on target
            for row in event.rows:
                values = row["values"]
                if values.get(self._tenant_col) != self._customer_id:
                    continue
                table = event.table
                cols  = self._schema_cache.columns(table)
                sql, args = _build_upsert(table, values, cols)
                self._batch.append((sql, args))

        elif isinstance(event, UpdateRowsEvent):
            # Rows updated on source → upsert after_values on target
            for row in event.rows:
                after = row["after_values"]
                if after.get(self._tenant_col) != self._customer_id:
                    continue
                table = event.table
                cols  = self._schema_cache.columns(table)
                sql, args = _build_upsert(table, after, cols)
                self._batch.append((sql, args))

        elif isinstance(event, DeleteRowsEvent):
            # Rows deleted on source → delete by PK on target
            for row in event.rows:
                values = row["values"]
                if values.get(self._tenant_col) != self._customer_id:
                    continue
                table  = event.table
                pk_col = self._schema_cache.pk_col(table)
                sql, args = _build_delete(table, pk_col, values)
                self._batch.append((sql, args))

    def run(self) -> None:
        """
        Open the binlog stream and process events until shutdown is requested.

        Registers SIGTERM and SIGINT handlers for graceful drain-and-stop.
        After the loop exits (signal or exception), the remaining batch is
        flushed and all connections are closed.
        """
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        coords = _load_checkpoint(self._state_dir)
        if coords is None:
            log_error(
                f"No checkpoint or binlog.coord found in {self._state_dir}/coords/. "
                "Run historical migration first."
            )
            sys.exit(1)

        log_file, log_pos = coords

        stream = BinLogStreamReader(
            connection_settings=self._src_settings,
            server_id=100,          # must differ from source server_id (=1)
            log_file=log_file,
            log_pos=log_pos,
            resume_stream=True,
            blocking=True,
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent],
            only_schemas=[self._src_db],
        )

        log_info(
            f"CDC stream open: {log_file} pos={log_pos}  "
            f"customer={self._customer_id} tenant_col={self._tenant_col}"
        )

        try:
            for event in stream:
                if not self._running:
                    break

                # Track the current stream position for checkpoint writes
                self._current_log_file = stream.log_file
                self._current_log_pos  = stream.log_pos

                self._process_event(event)
                self._maybe_flush()

        except KeyboardInterrupt:
            # Treat Ctrl-C the same as SIGINT
            self._running = False

        finally:
            # Always flush remaining events and close cleanly
            self._flush()
            stream.close()
            self._tgt_conn.close()
            log_info("CDC streamer stopped cleanly.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """
    CLI entry point for the CDC streamer.

    Parses arguments, loads the config, instantiates the CDCStreamer, and
    calls run().  The process exits naturally when the streamer stops.
    """
    args = _parse_args()

    with open(args.config) as f:
        config = json.load(f)

    streamer = CDCStreamer(
        config=config,
        customer_id=args.customer_id,
        tenant_column=args.tenant_column,
        state_dir=args.state_dir,
        batch_size=args.batch_size,
        commit_interval_s=args.commit_interval_s,
    )
    streamer.run()


if __name__ == "__main__":
    main()
