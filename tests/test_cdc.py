"""
test_cdc.py

After historical migration, start the CDC streamer, make DML changes on the
source, poll the target, confirm they land, restart the streamer after a kill,
and confirm checkpoint advanced.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import signal

import pymysql
import pymysql.cursors
import pytest


CUSTOMER = "acc_12345"
TENANT_COL = "account_id"
CDC_POLL_TIMEOUT = 30  # seconds to wait for CDC changes to appear on target
CDC_POLL_INTERVAL = 0.5


# ── helpers ───────────────────────────────────────────────────────────────────

def _src_conn(docker_stack):
    return pymysql.connect(
        host=docker_stack["src_host"],
        port=docker_stack["src_port"],
        user=docker_stack["src_user"],
        password=docker_stack["src_pw"],
        database=docker_stack["src_db"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _tgt_conn(docker_stack):
    return pymysql.connect(
        host=docker_stack["tgt_host"],
        port=docker_stack["tgt_port"],
        user=docker_stack["tgt_user"],
        password=docker_stack["tgt_pw"],
        database=docker_stack["tgt_db"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _poll_until(fn, timeout=CDC_POLL_TIMEOUT, interval=CDC_POLL_INTERVAL):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def _start_cdc(config_path, state_dir, env):
    return subprocess.Popen(
        [
            sys.executable, "-m", "marketo_migration.cdc",
            "--config", str(config_path),
            "--customer-id", CUSTOMER,
            "--tenant-column", TENANT_COL,
            "--state-dir", state_dir,
            "--batch-size", "50",
            "--commit-interval-s", "1.0",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def migrated_state(docker_stack, tmp_path_factory):
    """
    Run historical migration first (small chunk_size), return paths/config.
    """
    tmp = tmp_path_factory.mktemp("cdc_test")
    repo = docker_stack["repo_root"]

    config = {
        "source": {
            "host": docker_stack["src_host"],
            "port": docker_stack["src_port"],
            "user": docker_stack["src_user"],
            "database": docker_stack["src_db"],
            "container": docker_stack["src_container"],
        },
        "target": {
            "host": docker_stack["tgt_host"],
            "port": docker_stack["tgt_port"],
            "user": docker_stack["tgt_user"],
            "database": docker_stack["tgt_db"],
            "container": docker_stack["tgt_container"],
        },
        "work_dir": str(tmp / "work"),
        "state_dir": str(tmp / "state"),
        "chunk_size": 10000,
        "parallelism": 2,
        "tables": ["leads"],
        "exclude_tables": [],
    }

    config_path = tmp / "config.json"
    config_path.write_text(json.dumps(config))
    sh_path = tmp / "migrate.sh"
    plan_path = tmp / "plan.json"

    env = {**os.environ, "SRC_PASSWORD": docker_stack["src_pw"], "TGT_PASSWORD": docker_stack["tgt_pw"]}

    r = subprocess.run(
        [
            sys.executable, "-m", "marketo_migration.orchestrator",
            "--config", str(config_path),
            "--customer-id", CUSTOMER,
            "--tenant-column", TENANT_COL,
            "--out", str(sh_path),
            "--plan-json", str(plan_path),
        ],
        capture_output=True, text=True, env=env, cwd=str(repo),
    )
    assert r.returncode == 0, r.stderr

    r = subprocess.run(
        ["bash", str(sh_path)],
        env=env, cwd=str(repo), timeout=600,
    )
    assert r.returncode == 0

    return {
        "config": config,
        "config_path": str(config_path),
        "state_dir": str(tmp / "state"),
        "env": env,
    }


# ── tests ─────────────────────────────────────────────────────────────────────

class TestCDC:
    def test_insert_lands_on_target(self, docker_stack, migrated_state):
        state = migrated_state
        env = state["env"]

        # Start CDC
        proc = _start_cdc(state["config_path"], state["state_dir"], env)
        try:
            # Give the streamer a moment to connect
            time.sleep(3)

            # Insert a new lead on source
            src = _src_conn(docker_stack)
            with src.cursor() as cur:
                cur.execute(
                    "INSERT INTO leads (account_id, email, created_at) VALUES (%s, %s, NOW())",
                    (CUSTOMER, "cdc_insert_test@example.com"),
                )
                new_id = cur.lastrowid
            src.close()

            # Poll target
            tgt = _tgt_conn(docker_stack)
            found = _poll_until(
                lambda: _row_exists(tgt, "leads", new_id),
                timeout=CDC_POLL_TIMEOUT,
            )
            tgt.close()

            assert found, f"CDC insert not seen on target within {CDC_POLL_TIMEOUT}s"

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_update_lands_on_target(self, docker_stack, migrated_state):
        state = migrated_state
        env = state["env"]

        # Insert a lead first (without CDC, directly into both sides for simplicity)
        src = _src_conn(docker_stack)
        with src.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (account_id, email, created_at) VALUES (%s, %s, NOW())",
                (CUSTOMER, "cdc_update_before@example.com"),
            )
            test_id = cur.lastrowid
        src.close()

        # Also insert on target so the update has a row to hit
        tgt = _tgt_conn(docker_stack)
        with tgt.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO leads (id, account_id, email, created_at) VALUES (%s, %s, %s, NOW())",
                (test_id, CUSTOMER, "cdc_update_before@example.com"),
            )
        tgt.close()

        proc = _start_cdc(state["config_path"], state["state_dir"], env)
        try:
            time.sleep(3)

            # Update on source
            src = _src_conn(docker_stack)
            with src.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET email = %s WHERE id = %s",
                    ("cdc_update_after@example.com", test_id),
                )
            src.close()

            # Poll target for updated email
            tgt = _tgt_conn(docker_stack)
            found = _poll_until(
                lambda: _email_equals(tgt, "leads", test_id, "cdc_update_after@example.com"),
                timeout=CDC_POLL_TIMEOUT,
            )
            tgt.close()

            assert found, "CDC update not reflected on target"

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_delete_lands_on_target(self, docker_stack, migrated_state):
        state = migrated_state
        env = state["env"]

        # Insert into both sides
        src = _src_conn(docker_stack)
        with src.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (account_id, email, created_at) VALUES (%s, %s, NOW())",
                (CUSTOMER, "cdc_delete_test@example.com"),
            )
            del_id = cur.lastrowid
        src.close()

        tgt = _tgt_conn(docker_stack)
        with tgt.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO leads (id, account_id, email, created_at) VALUES (%s, %s, %s, NOW())",
                (del_id, CUSTOMER, "cdc_delete_test@example.com"),
            )
        tgt.close()

        proc = _start_cdc(state["config_path"], state["state_dir"], env)
        try:
            time.sleep(3)

            # Delete on source
            src = _src_conn(docker_stack)
            with src.cursor() as cur:
                cur.execute("DELETE FROM leads WHERE id = %s", (del_id,))
            src.close()

            # Poll target until row disappears
            tgt = _tgt_conn(docker_stack)
            gone = _poll_until(
                lambda: not _row_exists(tgt, "leads", del_id),
                timeout=CDC_POLL_TIMEOUT,
            )
            tgt.close()

            assert gone, "CDC delete not reflected on target"

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_checkpoint_advances_after_restart(self, docker_stack, migrated_state):
        """Kill CDC, restart, make a change, verify checkpoint file updated."""
        state = migrated_state
        env = state["env"]
        checkpoint_path = os.path.join(state["state_dir"], "coords", "cdc.checkpoint.json")

        # First run to establish checkpoint
        proc = _start_cdc(state["config_path"], state["state_dir"], env)
        time.sleep(4)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        assert os.path.exists(checkpoint_path), "No checkpoint file after first run"
        with open(checkpoint_path) as f:
            cp1 = json.load(f)
        pos1 = cp1["log_pos"]

        # Second run: make a change, wait for checkpoint to advance
        proc2 = _start_cdc(state["config_path"], state["state_dir"], env)
        try:
            time.sleep(3)

            src = _src_conn(docker_stack)
            with src.cursor() as cur:
                cur.execute(
                    "INSERT INTO leads (account_id, email, created_at) VALUES (%s, %s, NOW())",
                    (CUSTOMER, "cdc_checkpoint_test@example.com"),
                )
            src.close()

            # Wait for checkpoint to advance
            def _checkpoint_advanced():
                if not os.path.exists(checkpoint_path):
                    return False
                with open(checkpoint_path) as f:
                    cp = json.load(f)
                return cp["log_pos"] > pos1

            advanced = _poll_until(_checkpoint_advanced, timeout=CDC_POLL_TIMEOUT)
            assert advanced, f"Checkpoint did not advance beyond pos={pos1}"

        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc2.kill()
                proc2.wait()


# ── query helpers ─────────────────────────────────────────────────────────────

def _row_exists(conn: pymysql.Connection, table: str, row_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{table}` WHERE id = %s", (row_id,))
        return cur.fetchone() is not None


def _email_equals(conn: pymysql.Connection, table: str, row_id: int, email: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT email FROM `{table}` WHERE id = %s", (row_id,))
        row = cur.fetchone()
        return row is not None and row["email"] == email
