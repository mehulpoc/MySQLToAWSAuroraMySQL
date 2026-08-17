"""
test_historical_end_to_end.py

Generates the bash script, runs it, and verifies:
  - Every acc_12345 row lands on the target.
  - No acc_99999 rows bleed across.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import shutil
import time

import pymysql
import pymysql.cursors
import pytest


CUSTOMER = "acc_12345"
OTHER_CUSTOMER = "acc_99999"
TENANT_COL = "account_id"


@pytest.fixture(scope="module")
def config(docker_stack, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("historical_e2e")
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
        "chunk_size": 10000,
        "parallelism": 2,
        "tables": [],
        "exclude_tables": [],
    }


@pytest.fixture(scope="module")
def migrated(docker_stack, config, tmp_path_factory):
    """Run the full migration once for this module; yield state_dir."""
    tmp = tmp_path_factory.mktemp("historical_e2e_cfg")
    repo = docker_stack["repo_root"]

    config_path = tmp / "config.json"
    config_path.write_text(json.dumps(config))
    sh_path = tmp / "migrate.sh"
    plan_path = tmp / "plan.json"

    env = {**os.environ, "SRC_PASSWORD": docker_stack["src_pw"], "TGT_PASSWORD": docker_stack["tgt_pw"]}

    # Generate the bash script
    result = subprocess.run(
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
    assert result.returncode == 0, result.stderr

    # Run the generated script
    result = subprocess.run(
        ["bash", str(sh_path)],
        capture_output=False, text=True, env=env, cwd=str(repo),
        timeout=600,
    )
    assert result.returncode == 0, "Migration script exited non-zero"

    yield {**config, "src_pw": docker_stack["src_pw"], "tgt_pw": docker_stack["tgt_pw"]}


def _count(host, port, user, password, database, table, account_id):
    conn = pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, cursorclass=pymysql.cursors.DictCursor,
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM `{table}` WHERE account_id = %s",
            (account_id,),
        )
        result = cur.fetchone()["cnt"]
    conn.close()
    return result


class TestHistoricalMigration:
    def test_leads_migrated(self, migrated):
        cfg = migrated
        src_ep = cfg["source"]
        tgt_ep = cfg["target"]

        src_count = _count(
            src_ep["host"], src_ep["port"], src_ep["user"], cfg["src_pw"], src_ep["database"],
            "leads", CUSTOMER,
        )
        tgt_count = _count(
            tgt_ep["host"], tgt_ep["port"], tgt_ep["user"], cfg["tgt_pw"], tgt_ep["database"],
            "leads", CUSTOMER,
        )
        assert src_count > 0, "Source has no leads — seed problem?"
        assert src_count == tgt_count, f"leads: src={src_count} tgt={tgt_count}"

    def test_activities_migrated(self, migrated):
        cfg = migrated
        src_ep = cfg["source"]
        tgt_ep = cfg["target"]

        src_count = _count(
            src_ep["host"], src_ep["port"], src_ep["user"], cfg["src_pw"], src_ep["database"],
            "activities", CUSTOMER,
        )
        tgt_count = _count(
            tgt_ep["host"], tgt_ep["port"], tgt_ep["user"], cfg["tgt_pw"], tgt_ep["database"],
            "activities", CUSTOMER,
        )
        assert src_count == tgt_count, f"activities: src={src_count} tgt={tgt_count}"

    def test_no_cross_tenant_bleed_leads(self, migrated):
        """acc_99999 rows must NOT appear on the target."""
        cfg = migrated
        tgt_ep = cfg["target"]
        tgt_count = _count(
            tgt_ep["host"], tgt_ep["port"], tgt_ep["user"], cfg["tgt_pw"], tgt_ep["database"],
            "leads", OTHER_CUSTOMER,
        )
        assert tgt_count == 0, f"Cross-tenant bleed: {tgt_count} acc_99999 leads on target"

    def test_no_cross_tenant_bleed_activities(self, migrated):
        cfg = migrated
        tgt_ep = cfg["target"]
        tgt_count = _count(
            tgt_ep["host"], tgt_ep["port"], tgt_ep["user"], cfg["tgt_pw"], tgt_ep["database"],
            "activities", OTHER_CUSTOMER,
        )
        assert tgt_count == 0, f"Cross-tenant bleed: {tgt_count} acc_99999 activities on target"

    def test_binlog_coord_captured(self, migrated):
        state_dir = migrated["state_dir"]
        coord_path = os.path.join(state_dir, "coords", "binlog.coord")
        assert os.path.exists(coord_path), "binlog.coord not written"
        content = open(coord_path).read()
        assert "CHANGE MASTER TO" in content, f"Unexpected content: {content}"
        assert "MASTER_LOG_FILE" in content
        assert "MASTER_LOG_POS" in content
