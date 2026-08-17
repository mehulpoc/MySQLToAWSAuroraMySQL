# MySQL 5.7 → AWS Aurora MySQL Migration

Proof-of-concept tooling for migrating tenant data from an on-premise **MySQL 5.7** database to **AWS Aurora MySQL 2.x** (MySQL 5.7-compatible), with chunk-parallel bulk load, binlog-based CDC hand-off, and byte-exact checksum verification.

---

## Table of contents

1. [Architecture](#architecture)
2. [Repository structure](#repository-structure)
3. [Prerequisites](#prerequisites)
4. [Local POC — Docker targets](#local-poc--docker-targets)
5. [Aurora migration — real AWS target](#aurora-migration--real-aws-target)
   - [Option A: Terraform (Terraform Cloud state)](#option-a-terraform--terraform-cloud-state)
   - [Option B: AWS CDK](#option-b-aws-cdk)
   - [Running the migration against Aurora](#running-the-migration-against-aurora)
6. [Component reference](#component-reference)
7. [Configuration reference](#configuration-reference)
8. [Environment variables](#environment-variables)
9. [Test suite](#test-suite)
10. [Known limitations](#known-limitations)

---

## Architecture

```
┌─────────────────────────────┐         ┌──────────────────────────────────┐
│   On-Premise MySQL 5.7      │         │   AWS Aurora MySQL 2.x           │
│   (source)                  │         │   (target)                        │
│                             │         │                                  │
│  binlog: ROW format         │──bulk──▶│  marketo_tgt database            │
│  server-id: 1               │  load   │  rds_superuser privilege          │
│  miguser / REPLICATION      │         │  binlog_format = ROW             │
│  SLAVE privileges           │──CDC───▶│  Secrets Manager credentials     │
└─────────────────────────────┘         └──────────────────────────────────┘
         ▲
         │
┌────────┴──────────────────────────────────────────────────────────────────┐
│  Migration tooling (runs on any host with Docker + Python 3.12+)          │
│                                                                            │
│  orchestrator.py  — plan chunks, emit migrate.sh                          │
│  migrate.sh       — parallel chunk dump → load → parity check             │
│  cdc.py           — stream binlog events from source → apply to target    │
│  verify.py        — BIT_XOR(CRC32) row-count + checksum parity check      │
└───────────────────────────────────────────────────────────────────────────┘
```

**Migration is split into two phases:**

| Phase | Mechanism | Downtime |
|---|---|---|
| Historical bulk load | `mysqldump --replace --where "account_id=X"` chunked by PK range, parallel via `xargs -P` | Zero — source stays live |
| CDC hand-off | Binlog streamed from the coordinate captured in chunk 1 | Seconds — cut-over when lag = 0 |

See `docs/architecture.drawio` (5-page DrawIO) and `docs/MySQLToAuroraMySQL_Design.docx` for full design documentation.

---

## Repository structure

```
.
├── config.example.json          # Config for local Docker POC
├── config.aurora.example.json   # Config template for Aurora target
├── Makefile                     # Developer shortcuts (up/down/seed/reset/test/poc/cdk-*/tf-*)
├── pyproject.toml               # Python package metadata
├── requirements.txt             # PyMySQL, mysql-replication, pytest
│
├── docker/
│   └── compose.yaml             # Two MySQL 5.7 containers (source:3307, target:3308)
│
├── fixtures/
│   ├── source_schema.sql        # Source DB schema (leads, activities, email_preferences, …)
│   ├── target_schema.sql        # Target DB schema (identical structure)
│   └── seed.sql                 # ~52 k leads + ~21 k activities via cross-join trick
│
├── scripts/
│   ├── bootstrap.sh             # Bring up Docker stack + load fixtures (local POC)
│   ├── bootstrap_aurora.sh      # Bring up source container + prepare Aurora target
│   ├── run_poc.sh               # 6-step local Docker POC (end-to-end)
│   └── run_poc_aurora.sh        # 5-step Aurora POC (end-to-end)
│
├── src/marketo_migration/
│   ├── orchestrator.py          # Plan migration, emit self-contained migrate.sh
│   ├── cdc.py                   # Binlog CDC streamer (mysql-replication 0.46)
│   └── verify.py                # Row-count + BIT_XOR(CRC32) parity checker
│
├── tests/
│   ├── conftest.py              # Session-scoped Docker stack fixture
│   ├── test_planning.py         # Orchestrator chunk planning
│   ├── test_historical_end_to_end.py  # Bulk load + verify
│   ├── test_resume_from_kill.py # Kill-and-resume resumability
│   └── test_cdc.py              # CDC event streaming
│
├── infra/
│   ├── terraform/               # Terraform (Terraform Cloud state)
│   │   ├── modules/aurora/      # Shared resources: VPC, SG, Aurora cluster, Secrets Manager
│   │   └── environments/        # One root module + TFC workspace per env (dev/qa/staging/prod)
│   │       ├── dev/             # versions.tf (backend+provider), main.tf (module call + sizing),
│   │       ├── qa/               # variables.tf (pass-through overrides), outputs.tf — same 4
│   │       ├── staging/          # files in each, only main.tf's sizing/vpc_cidr differs
│   │       └── prod/             # separate AWS account from the other three
│   └── cdk/                     # AWS CDK (Python)
│       ├── app.py               # CDK app entry point — resolves environment + account/region
│       ├── aurora_stack.py      # AuroraStack definition
│       ├── cdk.json             # Context defaults + per-env (dev/qa/staging/prod) overrides
│       └── requirements.txt     # aws-cdk-lib, constructs
│
└── docs/
    ├── MySQLToAuroraMySQL_Design.docx  # Customer handover design document
```

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Docker Desktop | ≥ 4.x | Source MySQL 5.7 container (and target for local POC) |
| Python | ≥ 3.12 | Migration scripts, orchestrator, CDC, verify |
| pip | any | Install Python dependencies |
| mysql client | any (5.7 recommended) | Aurora target connections in `bootstrap_aurora.sh` |
| Terraform | ≥ 1.5 | Aurora infrastructure provisioning (Option A) |
| AWS CDK | ≥ 2.100 | Aurora infrastructure provisioning (Option B) |
| AWS CLI | ≥ 2.x | Retrieve credentials from Secrets Manager |

> **macOS note:** The system `mysql` client on macOS 13+ is version 9.x and dropped `mysql_native_password`. All source-side `mysql`/`mysqldump` calls go through the MySQL 5.7 Docker container (`docker exec`) to avoid client version mismatches. Only Aurora target connections use the host-side client.

---

## Local POC — Docker targets

The local POC runs entirely on Docker. Both source and target are MySQL 5.7 containers. No AWS account is needed.

### 1. Install Python dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 2. Run the full 6-step POC

```bash
make poc
```

This is equivalent to:

```bash
SRC_PASSWORD=migpw TGT_PASSWORD=tgtpw bash scripts/run_poc.sh
```

| Step | What it does |
|---|---|
| 1 | `make reset` — tears down Docker volumes, starts fresh containers, loads schema + 52 k seed rows |
| 2 | Orchestrator plans PK-range chunks for `acc_12345`, emits `migrate.sh` + `plan.json` |
| 3 | Executes `migrate.sh`: chunk 1 captures binlog coord, remaining chunks run in parallel |
| 4 | Kills the migration mid-run after 60 s, re-runs — completed chunks are skipped (`.done` markers) |
| 5 | `verify.py` compares row counts + `BIT_XOR(CRC32)` checksums between source and target |
| 6 | Runs the full pytest suite (21 tests) against a fresh Docker stack |

### 3. Individual make targets

```bash
make up      # Start containers (no seed)
make down    # Stop containers (preserve volumes)
make reset   # Wipe volumes, start fresh, load seed
make seed    # Load schema + seed into running containers
make test    # Run pytest suite only
```

### Default credentials (local Docker only)

| Role | User | Password | Database | Port |
|---|---|---|---|---|
| Source migration user | `miguser` | `migpw` | `marketo_src` | 3307 |
| Target write user | `tgtuser` | `tgtpw` | `marketo_tgt` | 3308 |
| Both containers root | `root` | `rootpw` | — | — |

---

## Aurora migration — real AWS target

For the real migration, the **source** remains the on-premise MySQL 5.7 Docker container (or your actual on-premise server), and the **target** is Aurora MySQL 2.x in AWS.

Two infrastructure-as-code options are provided — choose one.

---

### Option A: Terraform + Terraform Cloud state

Terraform Cloud stores the state file remotely and provides locking, audit history, and secret variable storage.

Like the CDK side, there are four deployable environments — `dev`, `qa`, `staging`, `prod` — but
Terraform has no built-in concept of a CloudFormation-style independent stack per deploy, so each
environment gets its **own root module directory** under `infra/terraform/environments/`, each
with its own Terraform Cloud workspace (state). All four call the same shared resource
definitions in `infra/terraform/modules/aurora/`, so there's no duplicated resource logic — only
the per-environment sizing (instance class, HA count, backup retention, deletion protection, VPC
CIDR) differs, and that's hardcoded in each environment's `main.tf` (not passed via `-var`), so a
bare `terraform apply` always reproduces the same shape for that environment.

```
infra/terraform/
├── modules/aurora/          # Shared resource definitions (VPC, SG, Aurora cluster, Secrets Manager, ...)
└── environments/
    ├── dev/                 # workspace: marketo-aurora-mysql-dev      (shared account)
    ├── qa/                  # workspace: marketo-aurora-mysql-qa       (shared account)
    ├── staging/              # workspace: marketo-aurora-mysql-staging (shared account)
    └── prod/                # workspace: marketo-aurora-mysql-prod    (separate account)
```

`dev`/`qa`/`staging` deploy into one shared AWS account; `prod` deploys into a separate account —
same split as the CDK side. Terraform's AWS provider has no account-resolution logic of its own;
it uses whatever credentials are active in your shell (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, SSO,
...), so always run `aws sts get-caller-identity` before applying to `prod` to confirm you're
pointed at the right account.

#### Initial setup

1. Create a free account at <https://app.terraform.io>
2. In each `infra/terraform/environments/<env>/versions.tf`, replace `REPLACE_WITH_YOUR_TF_CLOUD_ORG` with your organisation name, **or** set the environment variable:
   ```bash
   export TF_CLOUD_ORGANIZATION=your-org-name
   ```
3. Authenticate:
   ```bash
   terraform login
   ```

#### Deploy Aurora

```bash
cd infra/terraform/environments/dev
terraform init
terraform apply -var="allowed_cidr_blocks=[\"10.0.0.0/8\"]"
```

Or via the Makefile from the repo root (`ENV` defaults to `dev`; all four — `dev`/`qa`/`staging`/`prod` — are valid, matching the CDK side):

```bash
make tf-init  ENV=qa
make tf-plan  ENV=staging
make tf-apply ENV=prod TF_ARGS='-var="allowed_cidr_blocks=[\"10.0.0.0/8\"]"'
```

> `allowed_cidr_blocks` must include the CIDR of the host running the migration scripts (VPN subnet, EC2 subnet, etc.).

#### Key variables

Only these are meant to be overridden per-deploy (with `-var` / `-var-file`) without editing code:

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | AWS region |
| `project_name` | `marketo-migration` | Resource name prefix |
| `create_vpc` | `true` | Create a new VPC; set `false` to use `existing_vpc_id` |
| `existing_vpc_id` | `""` | Use existing VPC (when `create_vpc=false`) |
| `allowed_cidr_blocks` | `[]` | CIDRs allowed on port 3306 |
| `db_engine_version` | `5.7.mysql_aurora.2.12.1` | Aurora MySQL 2.x engine version |

Per-environment values (`environment`, `vpc_cidr`, `db_instance_class`, `cluster_instance_count`,
`backup_retention_period`, `deletion_protection`) are hardcoded in each environment's `main.tf` —
edit that file (or its `../../modules/aurora` defaults) to change them permanently, mirroring
`context.environments` in `infra/cdk/cdk.json`.

#### Retrieve outputs after apply

```bash
# View all outputs
terraform output

# Get the env-var block for the migration scripts
terraform output migration_env_vars

# Export TGT_PASSWORD from Secrets Manager
eval "$(terraform output -raw retrieve_tgt_password_command)"
```

#### Tear down

```bash
terraform destroy
# or: make tf-destroy ENV=dev
```

---

### Option B: AWS CDK

The CDK app deploys four environments — `dev`, `qa`, `staging`, `prod` — each as its own
CloudFormation stack (`{project_name}-{environment}-aurora`), selected with
`--context environment=<name>`. **`dev`/`qa`/`staging` share one AWS account; `prod` deploys into
a separate account.** Account IDs are never committed to git — they come from per-environment env
vars, falling back to `account` in `cdk.json`, falling back to whatever profile `cdk` runs under.

```bash
cd infra/cdk
pip install -r requirements.txt

# dev/qa/staging share one account — bootstrap it once
export AWS_ACCOUNT_DEV=111111111111
export AWS_ACCOUNT_QA=111111111111
export AWS_ACCOUNT_STAGING=111111111111
cdk bootstrap aws://111111111111/us-east-1

# prod is a separate account — bootstrap it too (needs prod-scoped AWS credentials)
export AWS_ACCOUNT_PROD=222222222222
cdk bootstrap aws://222222222222/us-east-1

# Preview / deploy a specific environment
cdk diff   --context environment=qa
cdk deploy --context environment=qa
```

Or via the Makefile from the repo root (`ENV` defaults to `dev`):

```bash
make cdk-diff   ENV=qa
make cdk-deploy ENV=staging
make cdk-deploy ENV=prod      # requires AWS credentials scoped to the prod account
```

#### Override context values without editing `cdk.json`

```bash
# One-off override on top of the staging environment's defaults
cdk deploy \
  --context environment=staging \
  --context cluster_instance_count=2 \
  --context deletion_protection=true \
  --context allowed_cidrs="10.0.0.0/8,192.168.100.0/24"

# Use existing VPC
cdk deploy \
  --context create_vpc=false \
  --context existing_vpc_id=vpc-0abc123456def \
  --context allowed_cidrs="10.0.0.0/8"
```

#### Key context values (in `cdk.json`)

| Key | Default | Description |
|---|---|---|
| `project_name` | `marketo-migration` | Resource name prefix |
| `environment` | `dev` | Deployment environment label (`dev`/`qa`/`staging`/`prod`); selects the block under `environments` below |
| `db_name` | `marketo_tgt` | Initial database name |
| `db_username` | `admin` | Master username |
| `aurora_engine_version` | `5.7.mysql_aurora.2.12.1` | Aurora MySQL 2.x engine version |

**Per-environment overrides** (`context.environments.<name>` in `cdk.json` — takes priority over the
flat keys above; each environment also fills in `account`/`region` for `app.py` if the matching
`AWS_ACCOUNT_*` env var isn't set):

| Key | Default | Description |
|---|---|---|
| `account` / `region` | `""` / `us-east-1` | AWS account/region for this environment; env var (`AWS_ACCOUNT_DEV` etc.) wins if set |
| `db_instance_class` | varies (`t4g.medium` dev/qa, `r6g.large` staging, `r6g.xlarge` prod) | Instance class (without `db.` prefix) |
| `cluster_instance_count` | `1` dev/qa, `2` staging/prod | `1` writer only; `2+` adds read replicas |
| `backup_retention_days` | `1` dev, `3` qa, `7` staging, `14` prod | Days of automated backup retention |
| `deletion_protection` | `false` dev/qa, `true` staging/prod | `true` also switches `removal_policy` to `SNAPSHOT` |
| `create_vpc` | `true` | `false` to use `existing_vpc_id` |
| `vpc_cidr` | one `/16` per env (`10.10/10.20/10.30/10.40.0.0/16`) | CIDR for new VPC |
| `allowed_cidrs` | `""` | Comma-separated CIDRs for port 3306 |

#### Tear down

```bash
cdk destroy --context environment=dev
# or: make cdk-destroy ENV=dev
```

---

### Running the migration against Aurora

After either Terraform or CDK provisions the Aurora cluster, run:

#### 1. Export connection variables

**From Terraform** (substitute the environment you deployed, e.g. `dev`):
```bash
eval "$(terraform -chdir=infra/terraform/environments/dev output -raw retrieve_tgt_password_command)"
eval "$(terraform -chdir=infra/terraform/environments/dev output -raw migration_env_vars)"
```

**From CDK / manually:**
```bash
export AURORA_HOST=your-cluster.cluster-xxxx.us-east-1.rds.amazonaws.com
export AURORA_PORT=3306
export AURORA_USER=admin
export AURORA_DB=marketo_tgt
export SRC_PASSWORD=migpw

# Retrieve auto-generated password from Secrets Manager:
export TGT_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id marketo-migration/dev/aurora-credentials \
  --query SecretString --output text \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])")
```

> Set `AURORA_SSL_CA=/path/to/rds-ca-bundle.pem` if your Aurora cluster requires SSL verification.

#### 2. Run the 5-step Aurora POC

```bash
source venv/bin/activate
pip install -e .

bash scripts/run_poc_aurora.sh
```

| Step | What it does |
|---|---|
| 1 | `bootstrap_aurora.sh` — starts source Docker container, seeds it, creates `marketo_tgt` on Aurora, loads target schema, grants `rds_superuser` |
| 2 | Orchestrator emits `migrate_aurora.sh` targeting Aurora directly (no docker exec on target) |
| 3 | Executes `migrate_aurora.sh`: bulk load source → Aurora in parallel chunks |
| 4 | Kill-and-resume demonstration against Aurora |
| 5 | `verify.py` compares row counts + checksums between source container and Aurora |

#### 3. CDC hand-off (optional, for near-zero downtime)

After the bulk load completes, start CDC to replay binlog events that arrived since chunk 1:

```bash
python -m marketo_migration.cdc \
  --config /tmp/marketo_poc_aurora/config_aurora.json \
  --coord  /tmp/marketo_poc_aurora/migration_state/coords/binlog.coord \
  --customer-id   acc_12345 \
  --tenant-column account_id
```

CDC streams from the binlog coordinate captured during the bulk load, applies `INSERT`/`UPDATE`/`DELETE` events to Aurora, and checkpoints atomically after each batch. Stop it when source binlog lag reaches zero and your application is ready to cut over.

---

## Component reference

### `orchestrator.py`

Plans the migration for one customer and emits a self-contained bash script.

```bash
python -m marketo_migration.orchestrator \
  --config        config.example.json \
  --customer-id   acc_12345 \
  --tenant-column account_id \
  --out           /tmp/migrate.sh \
  --plan-json     /tmp/plan.json
```

**Target modes** (determined by presence of `target.container` in config):

| Mode | Trigger | Target connection |
|---|---|---|
| `docker` | `target.container` key present | `docker exec -i $TGT_CONTAINER mysql` |
| `direct` | No `target.container` key | `mysql -h $TGT_HOST -P $TGT_PORT` |

### `cdc.py`

Streams binlog events from the source MySQL server and applies them to the target.

```bash
python -m marketo_migration.cdc \
  --config        config.example.json \
  --coord         /tmp/state/coords/binlog.coord \
  --customer-id   acc_12345 \
  --tenant-column account_id
```

- Uses `mysql-replication==0.46` (last version compatible with MySQL 5.7)
- Checkpoint written atomically via `os.replace()` after each committed batch
- `server_id=100` — must differ from source `server_id=1`

### `verify.py`

Compares row counts and `BIT_XOR(CRC32(CONCAT_WS(...)))` checksums per table for a given customer slice.

```bash
python -m marketo_migration.verify \
  --config        config.example.json \
  --customer-id   acc_12345 \
  --tenant-column account_id \
  --plan-json     /tmp/plan.json   # optional: excludes orchestrator-skipped tables
```

Exits `0` if all tables match, `1` if any divergence is found.

---

## Configuration reference

### `config.example.json` — local Docker POC

```jsonc
{
  "source": {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "miguser",
    "database": "marketo_src",
    "container": "marketo-src"   // docker exec target for mysqldump
  },
  "target": {
    "host": "127.0.0.1",
    "port": 3308,
    "user": "tgtuser",
    "database": "marketo_tgt",
    "container": "marketo-tgt"   // docker exec target for mysql load
  },
  "work_dir":   "/tmp/marketo_migration/work",   // temp dump files
  "state_dir":  "/tmp/marketo_migration/state",  // .done markers + binlog coord
  "chunk_size": 5000,                            // rows per mysqldump chunk
  "parallelism": 4                               // xargs -P concurrency
}
```

### `config.aurora.example.json` — Aurora target

```jsonc
{
  "source": {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "miguser",
    "database": "marketo_src",
    "container": "marketo-src"   // source always uses docker exec
  },
  "target": {
    "host": "your-cluster.cluster-xxxx.rds.amazonaws.com",
    "port": 3306,
    "user": "admin",
    "database": "marketo_tgt"
    // no "container" key → orchestrator uses direct TCP connection
  },
  "work_dir":   "/tmp/marketo_migration/work",
  "state_dir":  "/tmp/marketo_migration/state",
  "chunk_size": 5000,
  "parallelism": 4
}
```

> The absence of `target.container` is the switch that tells the orchestrator to emit direct `mysql -h … -P …` commands instead of `docker exec`.

---

## Environment variables

All passwords are passed via environment variables. They are written to a mode-0600 temporary file (`--defaults-file`) and never appear on any command line.

| Variable | Required for | Description |
|---|---|---|
| `SRC_PASSWORD` | All scripts | Password for `miguser` on the source MySQL |
| `TGT_PASSWORD` | All scripts | Password for the target MySQL user |
| `AURORA_HOST` | Aurora scripts | Aurora cluster writer endpoint |
| `AURORA_PORT` | Aurora scripts | Aurora port (default: `3306`) |
| `AURORA_USER` | Aurora scripts | Aurora master user or `rds_superuser` account |
| `AURORA_DB` | Aurora scripts | Target database name (default: `marketo_tgt`) |
| `AURORA_SSL_CA` | Aurora scripts | Path to RDS CA bundle (optional; omit for private VPC) |

---

## Test suite

The pytest suite (21 tests) spins up its own Docker stack via a session-scoped fixture and is independent of any manual POC runs.

```bash
# Activate venv first
source venv/bin/activate

# Run all tests
make test

# Or directly:
pytest -x -v tests/
```

| Module | Coverage |
|---|---|
| `test_planning.py` | Orchestrator table discovery, chunk calculation, plan JSON |
| `test_historical_end_to_end.py` | Full bulk load + verify for `acc_12345` |
| `test_resume_from_kill.py` | Kill mid-run, verify `.done` markers, re-run skips completed chunks |
| `test_cdc.py` | Insert on source, confirm CDC delivers event to target within 30 s |

**Critical dependency note:** `mysql-replication==0.46` is the last pre-1.x version and the only version compatible with MySQL 5.7. Versions ≥ 1.0 require MySQL ≥ 8.0.14. `setuptools>=68` is required alongside it to provide the `distutils` compatibility shim removed in Python 3.12+.

---

## Known limitations

| Limitation | Impact | Workaround |
|---|---|---|
| Tables with composite primary keys are skipped by the orchestrator | Data in those tables is not bulk-loaded | Listed in `plan.json` under `skipped`; verify with `--plan-json` to exclude them from parity checks |
| CDC does not handle DDL changes (ALTER TABLE, etc.) | Schema changes on source during migration break replication | Freeze schema changes for the duration of the migration window |
| `SET SESSION sql_log_bin=0` on Aurora requires `rds_superuser` | Bulk load fails if the target user lacks this privilege | Use the Aurora master user, or `GRANT rds_superuser TO 'user'@'%'` |
| Aurora target connections require a host-side `mysql` client | `bootstrap_aurora.sh` fails if no `mysql` binary is on PATH | Install `mysql-client` on the migration host, or use an EC2 instance with it pre-installed |
| Chunk resumability uses local `.done` files | Moving the migration to a different host loses progress | Keep `state_dir` on a shared/persistent volume when running across hosts |
| Local Docker uses `mysql_native_password`; mysql 9.x client dropped it | Direct host-side connections to the Docker source fail | All source connections go through `docker exec` (5.7 client inside the container) |
