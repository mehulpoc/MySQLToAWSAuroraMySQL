"""
Session-scoped fixture: starts Docker Compose stack, loads schema and seed,
tears down after the session.

mysql/mysqldump are run via docker exec (container-local 5.7 tools).
Python connections use PyMySQL which speaks the MySQL protocol natively and
does not depend on host-installed client binaries or plugins.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pymysql
import pymysql.cursors
from pymysql.constants import CLIENT
import pytest

REPO_ROOT = Path(__file__).parent.parent

SRC_HOST = "127.0.0.1"
SRC_PORT = 3307
TGT_HOST = "127.0.0.1"
TGT_PORT = 3308
ROOT_PW = "rootpw"
SRC_USER = "miguser"
SRC_PW = "migpw"
TGT_USER = "tgtuser"
TGT_PW = "tgtpw"
SRC_DB = "marketo_src"
TGT_DB = "marketo_tgt"
SRC_CONTAINER = "marketo-src"
TGT_CONTAINER = "marketo-tgt"


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wait_for_mysql(host: str, port: int, timeout: int = 90) -> None:
    """
    Poll a MySQL endpoint until it accepts connections three times in a row.

    Three consecutive successes are required to survive the MySQL Docker
    two-phase initialisation restart — the server briefly accepts connections
    during init phase 1, then restarts for phase 2, creating a window where
    a single success would be a false positive.

    Args:
        host:    Hostname or IP of the MySQL server.
        port:    TCP port of the MySQL server.
        timeout: Maximum total seconds to wait before raising TimeoutError.

    Raises:
        TimeoutError: If the server is not ready within the timeout period.
    """
    deadline = time.time() + timeout
    consec = 0

    log_info(f"Waiting for MySQL at {host}:{port} (up to {timeout}s)...")

    while time.time() < deadline:
        try:
            # Attempt a lightweight connection to check liveness
            conn = pymysql.connect(
                host=host,
                port=port,
                user="root",
                password=ROOT_PW,
                connect_timeout=3,
            )
            conn.close()
            consec += 1

            # Require 3 consecutive successes before declaring ready
            if consec >= 3:
                log_info(f"MySQL at {host}:{port} is ready.")
                return

        except Exception:
            # Any failure resets the consecutive counter
            consec = 0

        time.sleep(1)

    raise TimeoutError(f"MySQL at {host}:{port} not ready after {timeout}s")


def _exec_sql_file_via_docker(
    container: str,
    user: str,
    pw: str,
    sql_path: Path,
) -> None:
    """
    Execute a SQL file inside a Docker container by piping it through stdin.

    Runs mysql inside the container so the host's MySQL client version and
    authentication plugin configuration are irrelevant.

    Args:
        container: Docker container name or ID.
        user:      MySQL user to authenticate as.
        pw:        Password for the MySQL user.
        sql_path:  Local path to the SQL file to execute.
    """
    log_info(f"  Executing SQL file {sql_path.name} in container {container}...")
    with open(sql_path) as f:
        subprocess.run(
            [
                "docker", "exec",
                "-e", f"MYSQL_PWD={pw}",
                "-i", container,
                "mysql", "-u", user,
            ],
            stdin=f,
            check=True,
            capture_output=True,
        )


def _root_query_via_docker(container: str, query: str) -> None:
    """
    Execute an arbitrary SQL statement inside a Docker container as root.

    Suitable for GRANT, FLUSH PRIVILEGES, and other administrative DDL that
    requires elevated privileges.  Output is suppressed (-sN flag).

    Args:
        container: Docker container name or ID.
        query:     SQL statement to execute.
    """
    subprocess.run(
        [
            "docker", "exec",
            "-e", f"MYSQL_PWD={ROOT_PW}",
            container,
            "mysql", "-u", "root", "-sN", "-e", query,
        ],
        check=True,
        capture_output=True,
    )


# ── Session fixture ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def docker_stack():
    """
    Session-scoped pytest fixture that manages the full Docker Compose stack.

    On setup:
      1. Tears down any existing stack and volumes for a clean state.
      2. Starts both MySQL containers (source on 3307, target on 3308).
      3. Waits for both to be ready (3 consecutive connection successes).
      4. Grants necessary privileges to miguser and tgtuser.
      5. Loads source schema, seed data, and target schema from fixtures/.

    Yields a dict of connection parameters for use by other fixtures.

    On teardown:
      Tears down the stack and volumes so the host is left clean.
    """
    compose_file = REPO_ROOT / "docker" / "compose.yaml"

    # Tear down any pre-existing stack so tests always start from a known state
    log_info("Tearing down existing Docker stack (if any)...")
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down", "-v"],
        check=False,
        capture_output=True,
    )

    # Start both MySQL containers in detached mode
    log_info("Starting Docker Compose stack...")
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        check=True,
    )

    # Wait until both databases are accepting connections
    _wait_for_mysql(SRC_HOST, SRC_PORT)
    _wait_for_mysql(TGT_HOST, TGT_PORT)

    # Grant replication and read rights on source to miguser
    log_info("Granting source privileges to miguser...")
    _root_query_via_docker(
        SRC_CONTAINER,
        "GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT "
        "ON *.* TO 'miguser'@'%'; FLUSH PRIVILEGES;",
    )

    # Grant SUPER on target to tgtuser so it can run SET SESSION sql_log_bin=0
    log_info("Granting target SUPER privilege to tgtuser...")
    _root_query_via_docker(
        TGT_CONTAINER,
        "GRANT SUPER ON *.* TO 'tgtuser'@'%'; FLUSH PRIVILEGES;",
    )

    # Load source schema, seed data (52 k leads + activities), and target schema
    log_info("Loading source schema...")
    _exec_sql_file_via_docker(
        SRC_CONTAINER, "root", ROOT_PW,
        REPO_ROOT / "fixtures" / "source_schema.sql",
    )

    log_info("Loading seed data (may take 30-90 s under emulation)...")
    _exec_sql_file_via_docker(
        SRC_CONTAINER, "root", ROOT_PW,
        REPO_ROOT / "fixtures" / "seed.sql",
    )

    log_info("Loading target schema...")
    _exec_sql_file_via_docker(
        TGT_CONTAINER, "root", ROOT_PW,
        REPO_ROOT / "fixtures" / "target_schema.sql",
    )

    log_info("Docker stack ready. Yielding to tests.")

    yield {
        "src_host": SRC_HOST,
        "src_port": SRC_PORT,
        "src_db": SRC_DB,
        "src_user": SRC_USER,
        "src_pw": SRC_PW,
        "tgt_host": TGT_HOST,
        "tgt_port": TGT_PORT,
        "tgt_db": TGT_DB,
        "tgt_user": TGT_USER,
        "tgt_pw": TGT_PW,
        "src_container": SRC_CONTAINER,
        "tgt_container": TGT_CONTAINER,
        "repo_root": REPO_ROOT,
    }

    # Teardown: remove containers and volumes after the test session completes
    log_info("Tearing down Docker stack after test session...")
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down", "-v"],
        check=False,
        capture_output=True,
    )


# ── PyMySQL connection fixtures ───────────────────────────────────────────────

@pytest.fixture(scope="session")
def src_conn(docker_stack):
    """
    Session-scoped PyMySQL connection to the source database (marketo_src).

    Authenticated as miguser which has SELECT and replication rights.
    Uses DictCursor so rows are returned as dicts keyed by column name.

    Args:
        docker_stack: Session fixture that ensures the containers are up.

    Yields:
        An open pymysql.Connection to the source database.
    """
    conn = pymysql.connect(
        host=SRC_HOST,
        port=SRC_PORT,
        user=SRC_USER,
        password=SRC_PW,
        database=SRC_DB,
        cursorclass=pymysql.cursors.DictCursor,
    )
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def tgt_conn(docker_stack):
    """
    Session-scoped PyMySQL connection to the target database (marketo_tgt).

    Authenticated as tgtuser which has INSERT/UPDATE/DELETE and SUPER rights.
    Uses DictCursor so rows are returned as dicts keyed by column name.

    Args:
        docker_stack: Session fixture that ensures the containers are up.

    Yields:
        An open pymysql.Connection to the target database.
    """
    conn = pymysql.connect(
        host=TGT_HOST,
        port=TGT_PORT,
        user=TGT_USER,
        password=TGT_PW,
        database=TGT_DB,
        cursorclass=pymysql.cursors.DictCursor,
    )
    yield conn
    conn.close()
