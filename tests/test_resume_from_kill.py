"""
test_resume_from_kill.py

Start the bash script, SIGKILL it after a short delay, count .done markers,
re-run, assert that completed chunks are skipped (SKIP lines in log) and
that the final row counts are correct.
"""
from __future__ import annotations

import glob
import json
import os
import signal
import subprocess
import sys
import time
import re

import pymysql
import pymysql.cursors
import pytest


CUSTOMER = "acc_12345"
TENANT_COL = "account_id"


@pytest.fixture(scope="module")
def config(docker_stack, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("resume_test")
    return {
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
        "chunk_size": 5000,   # smaller → more chunks → more useful kill test
        "parallelism": 2,
        "tables": ["leads"],  # limit to one table for speed
        "exclude_tables": [],
    }


def _setup_script(docker_stack, config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("resume_script")
    repo = docker_stack["repo_root"]

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

    return sh_path, plan_path, env


class TestResumeFromKill:
    def test_resume_skips_completed_chunks(self, docker_stack, config, tmp_path_factory):
        sh_path, plan_path, env = _setup_script(docker_stack, config, tmp_path_factory)
        state_dir = config["state_dir"]
        os.makedirs(state_dir, exist_ok=True)

        # ── First run: kill after a few seconds ──────────────────────────────
        proc = subprocess.Popen(
            ["bash", str(sh_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Give it time to complete at least a few chunks
        time.sleep(8)
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.wait()

        # Count .done markers written before kill
        done_markers_after_kill = glob.glob(os.path.join(state_dir, "*.done"))
        assert len(done_markers_after_kill) > 0, (
            "No .done markers found after kill — "
            "chunks may be too large or kill happened too early"
        )
        n_done_first = len(done_markers_after_kill)

        # ── Second run: should complete ──────────────────────────────────────
        proc2 = subprocess.run(
            ["bash", str(sh_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
        )
        assert proc2.returncode == 0, f"Second run failed:\n{proc2.stdout}"

        log_output = proc2.stdout

        # Assert SKIP lines in log equal the number of pre-existing markers
        skip_count = len(re.findall(r"SKIP .* marker exists", log_output))
        assert skip_count == n_done_first, (
            f"Expected {n_done_first} SKIP lines, found {skip_count}.\n"
            f"Log tail:\n{log_output[-3000:]}"
        )

        # ── Final row-count parity ───────────────────────────────────────────
        src_ep = config["source"]
        tgt_ep = config["target"]

        src_conn = pymysql.connect(
            host=src_ep["host"], port=src_ep["port"],
            user=src_ep["user"], password=docker_stack["src_pw"],
            database=src_ep["database"],
            cursorclass=pymysql.cursors.DictCursor,
        )
        tgt_conn = pymysql.connect(
            host=tgt_ep["host"], port=tgt_ep["port"],
            user=tgt_ep["user"], password=docker_stack["tgt_pw"],
            database=tgt_ep["database"],
            cursorclass=pymysql.cursors.DictCursor,
        )

        with src_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM leads WHERE account_id = %s",
                (CUSTOMER,),
            )
            src_count = cur.fetchone()["cnt"]

        with tgt_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM leads WHERE account_id = %s",
                (CUSTOMER,),
            )
            tgt_count = cur.fetchone()["cnt"]

        src_conn.close()
        tgt_conn.close()

        assert src_count == tgt_count, (
            f"Row count mismatch after resume: src={src_count} tgt={tgt_count}"
        )
