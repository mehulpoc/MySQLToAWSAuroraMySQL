"""
test_planning.py — orchestrator produces the expected chunk plan.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from marketo_migration.orchestrator import (
    _compute_chunks,
    _pk_info,
    _table_stats,
    _tables_with_tenant_col,
    _connect,
    _load_config,
)

CUSTOMER_12345 = "acc_12345"
TENANT_COL = "account_id"
CHUNK_SIZE = 5000
EXPECTED_LEADS_ROWS = 52_000
EXPECTED_ACTIVITIES_ROWS = 52_000


@pytest.fixture(scope="module")
def config(docker_stack):
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
        "work_dir": "/tmp/marketo_migration_test/work",
        "state_dir": "/tmp/marketo_migration_test/state",
        "chunk_size": CHUNK_SIZE,
        "parallelism": 2,
        "tables": [],
        "exclude_tables": [],
    }


@pytest.fixture(scope="module")
def src_connection(config, docker_stack):
    os.environ["SRC_PASSWORD"] = docker_stack["src_pw"]
    conn = _connect(config["source"], "SRC_PASSWORD")
    yield conn
    conn.close()


class TestTableDiscovery:
    def test_finds_tenant_tables(self, src_connection, config):
        tables = _tables_with_tenant_col(
            src_connection,
            config["source"]["database"],
            TENANT_COL,
            include=[],
            exclude=[],
        )
        assert "leads" in tables
        assert "activities" in tables
        assert "email_preferences" in tables

    def test_include_filter(self, src_connection, config):
        tables = _tables_with_tenant_col(
            src_connection,
            config["source"]["database"],
            TENANT_COL,
            include=["leads"],
            exclude=[],
        )
        assert tables == ["leads"]

    def test_exclude_filter(self, src_connection, config):
        tables = _tables_with_tenant_col(
            src_connection,
            config["source"]["database"],
            TENANT_COL,
            include=[],
            exclude=["activities"],
        )
        assert "activities" not in tables
        assert "leads" in tables


class TestPKInspection:
    def test_leads_is_chunkable(self, src_connection, config):
        pk_col, chunkable, reason = _pk_info(src_connection, config["source"]["database"], "leads")
        assert chunkable is True
        assert pk_col == "id"
        assert reason == ""

    def test_email_preferences_is_not_chunkable(self, src_connection, config):
        """Composite PK must be detected and rejected."""
        pk_col, chunkable, reason = _pk_info(
            src_connection, config["source"]["database"], "email_preferences"
        )
        assert chunkable is False
        assert "composite" in reason.lower()


class TestTableStats:
    def test_leads_stats(self, src_connection, config):
        stats = _table_stats(
            src_connection,
            config["source"]["database"],
            "leads",
            "id",
            TENANT_COL,
            CUSTOMER_12345,
        )
        assert stats is not None
        min_pk, max_pk, total_rows = stats
        assert total_rows >= EXPECTED_LEADS_ROWS
        assert min_pk >= 1
        assert max_pk >= min_pk

    def test_no_rows_returns_none(self, src_connection, config):
        stats = _table_stats(
            src_connection,
            config["source"]["database"],
            "leads",
            "id",
            TENANT_COL,
            "acc_nonexistent",
        )
        assert stats is None


class TestChunkComputation:
    def test_chunk_count_density_one(self):
        # Dense table: each PK has a row
        chunks = _compute_chunks(1, 52000, 52000, 5000)
        assert len(chunks) == 11  # ceil(52000/5000)
        assert chunks[0] == (1, 5000)
        assert chunks[-1][1] == 52000

    def test_chunks_cover_full_range(self):
        min_pk, max_pk, rows = 1, 10000, 5000
        chunks = _compute_chunks(min_pk, max_pk, rows, 1000)
        assert chunks[0][0] == min_pk
        assert chunks[-1][1] == max_pk
        # No gaps or overlaps
        for i in range(len(chunks) - 1):
            assert chunks[i][1] + 1 == chunks[i + 1][0]

    def test_sparse_pk_widens_chunk(self):
        # 1000 rows spread over 10000 PK range (10% density)
        # chunk_size=1000, density=0.1, step = ceil(1000/0.1) = 10000
        chunks = _compute_chunks(1, 10000, 1000, 1000)
        assert len(chunks) == 1
        assert chunks[0] == (1, 10000)


class TestOrchestratorEndToEnd:
    def test_produces_plan_json(self, config, tmp_path, docker_stack):
        """Full run through the orchestrator produces a sane plan."""
        import subprocess
        import sys

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        out_sh = tmp_path / "migrate.sh"
        out_json = tmp_path / "plan.json"

        env = {**os.environ, "SRC_PASSWORD": docker_stack["src_pw"], "TGT_PASSWORD": docker_stack["tgt_pw"]}
        result = subprocess.run(
            [
                sys.executable, "-m", "marketo_migration.orchestrator",
                "--config", str(config_path),
                "--customer-id", CUSTOMER_12345,
                "--tenant-column", TENANT_COL,
                "--out", str(out_sh),
                "--plan-json", str(out_json),
            ],
            capture_output=True, text=True, env=env,
            cwd=str(docker_stack["repo_root"]),
        )
        assert result.returncode == 0, result.stderr

        plan = json.loads(out_json.read_text())
        assert plan["customer_id"] == CUSTOMER_12345
        assert "leads" in plan["tables"]
        assert "activities" in plan["tables"]

        # email_preferences must be in skipped (composite PK)
        skipped_tables = {s["table"] for s in plan["skipped"]}
        assert "email_preferences" in skipped_tables

        # Chunk count sanity
        leads_chunks = plan["tables"]["leads"]["chunk_count"]
        assert leads_chunks >= 10, f"Expected ≥10 lead chunks, got {leads_chunks}"

        assert out_sh.exists()
        assert out_sh.stat().st_mode & 0o111  # executable
