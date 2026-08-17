"""
AuroraStack — CDK stack that provisions an Aurora MySQL 2.x cluster compatible
with an on-premise MySQL 5.7 source.

Resources created:
  - VPC with two PRIVATE_ISOLATED subnets across two AZs  (or looks up existing VPC)
  - Security group allowing port 3306 from configurable CIDR blocks
  - Aurora cluster parameter group  (binlog_format=ROW, binlog_checksum=NONE)
  - DB instance parameter group     (aurora-mysql5.7 family, default settings)
  - Secrets Manager secret          (auto-generated master credentials)
  - Aurora MySQL 2.x cluster        (writer + optional readers)
  - CfnOutputs for cluster endpoint, secret ARN, and migration env-var block

Context keys (set in cdk.json or via --context on the CLI):
  project_name            Prefix for all resource names         (default: marketo-migration)
  environment             dev | qa | staging | prod               (default: dev)
  db_name                 Initial database name                  (default: marketo_tgt)
  db_username             Master username                        (default: admin)
  aurora_engine_version   5.7.mysql_aurora.2.x.y string         (default: 5.7.mysql_aurora.2.12.1)
  db_instance_class       Instance class without 'db.' prefix    (default: r6g.large)
  cluster_instance_count  1 = writer only, 2+ = HA              (default: 1)
  backup_retention_days   Days of automated backups to retain   (default: 7)
  deletion_protection     true | false                           (default: false)
  create_vpc              true | false                           (default: true)
  vpc_cidr                CIDR for new VPC                       (default: 10.10.0.0/16)
  existing_vpc_id         Existing VPC ID (when create_vpc=false)
  allowed_cidrs           Comma-separated CIDRs for port 3306   (default: VPC CIDR)
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_rds as rds,
)
from constructs import Construct


class AuroraStack(Stack):
    """CDK stack for the Aurora MySQL 5.7-compatible migration target."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        environment: str,
        config: dict | None = None,
        **kwargs,
    ) -> None:
        """
        Initialise the AuroraStack.

        Args:
            scope:        CDK app or parent construct.
            construct_id: CloudFormation stack logical name.
            project_name: Prefix used in all resource names.
            environment:  Deployment environment label (dev/qa/staging/prod).
            config:       Per-environment overrides from
                          context.environments.<environment> in cdk.json
                          (instance size, HA count, deletion protection, ...).
                          Takes priority over the flat cdk.json context keys,
                          which remain as project-wide fallbacks.
            **kwargs:     Passed through to Stack (env=, description=, etc.).
        """
        super().__init__(scope, construct_id, **kwargs)

        # ── Read context values ───────────────────────────────────────────────
        # Per-environment config wins; falls back to flat top-level context
        # keys (project-wide defaults) when a key isn't set for this env.
        config = config or {}
        ctx = lambda key: config.get(key) or self.node.try_get_context(key)

        db_name             = ctx("db_name")              or "marketo_tgt"
        db_username         = ctx("db_username")          or "admin"
        engine_version_str  = ctx("aurora_engine_version") or "5.7.mysql_aurora.2.12.1"
        instance_class_str  = ctx("db_instance_class")    or "r6g.large"
        instance_count      = int(ctx("cluster_instance_count") or "1")
        backup_days         = int(ctx("backup_retention_days")  or "7")
        deletion_protection = str(ctx("deletion_protection") or "false").lower() == "true"
        create_vpc_flag     = str(ctx("create_vpc")       or "true").lower() == "true"
        vpc_cidr            = ctx("vpc_cidr")             or "10.10.0.0/16"
        existing_vpc_id     = ctx("existing_vpc_id")      or None
        allowed_cidrs_raw   = ctx("allowed_cidrs")        or ""

        # Parse comma-separated CIDR list; defaults are applied after VPC creation
        allowed_cidrs: list[str] = [
            c.strip() for c in allowed_cidrs_raw.split(",") if c.strip()
        ]

        prefix = f"{project_name}-{environment}"
        engine = rds.DatabaseClusterEngine.aurora_mysql(
            version=rds.AuroraMysqlEngineVersion.of(engine_version_str)
        )

        # ── VPC ───────────────────────────────────────────────────────────────
        if create_vpc_flag or not existing_vpc_id:
            # Create a dedicated VPC with two PRIVATE_ISOLATED subnets across two AZs.
            # PRIVATE_ISOLATED means no NAT gateway and no internet gateway — Aurora
            # is reachable only from within the VPC (via VPN, Direct Connect, or EC2).
            vpc = ec2.Vpc(
                self, "Vpc",
                vpc_name=f"{prefix}-vpc",
                ip_addresses=ec2.IpAddresses.cidr(vpc_cidr),
                max_azs=2,
                nat_gateways=0,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="private",
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=24,
                    )
                ],
            )
            # Default allowed CIDR to VPC CIDR when the caller left it empty
            if not allowed_cidrs:
                allowed_cidrs = [vpc_cidr]
        else:
            # Look up an existing VPC by ID. The VPC must have at least two private
            # subnets in different AZs to satisfy Aurora's DB subnet group requirement.
            vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=existing_vpc_id)

        # ── Security group ────────────────────────────────────────────────────
        # Restricts Aurora access to the listed CIDRs on port 3306.
        # Add the CIDR of your migration host (or VPN subnet) to allowed_cidrs.
        aurora_sg = ec2.SecurityGroup(
            self, "AuroraSg",
            vpc=vpc,
            security_group_name=f"{prefix}-aurora-sg",
            description=f"Allow port 3306 to Aurora from migration hosts ({prefix})",
            allow_all_outbound=True,
        )
        for cidr in allowed_cidrs:
            aurora_sg.add_ingress_rule(
                peer=ec2.Peer.ipv4(cidr),
                connection=ec2.Port.tcp(3306),
                description=f"MySQL from {cidr}",
            )

        # ── Cluster parameter group ───────────────────────────────────────────
        # binlog_format = ROW enables binary logging so Aurora can serve as a CDC
        # replication source in future failback or re-migration scenarios.
        # binlog_checksum = NONE matches the on-premise MySQL 5.7 default and is
        # required for compatibility with the mysql-replication Python library.
        cluster_pg = rds.ParameterGroup(
            self, "ClusterPg",
            engine=engine,
            description=f"Cluster PG for {prefix}: binlog ROW, checksum NONE",
            parameters={
                "binlog_format":   "ROW",
                "binlog_checksum": "NONE",
            },
        )

        # ── DB instance parameter group ───────────────────────────────────────
        # Applied per Aurora instance; kept at aurora-mysql5.7 family defaults.
        instance_pg = rds.ParameterGroup(
            self, "InstancePg",
            engine=engine,
            description=f"Instance PG for {prefix} Aurora MySQL 5.7",
        )

        # ── Credentials (auto-generated, stored in Secrets Manager) ───────────
        # CDK generates a strong random password and stores the full connection
        # bundle in Secrets Manager under {prefix}/aurora-credentials.
        # Retrieve the password with:
        #   aws secretsmanager get-secret-value \
        #       --secret-id <prefix>/aurora-credentials \
        #       --query SecretString --output text \
        #     | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])"
        db_credentials = rds.Credentials.from_generated_secret(
            username=db_username,
            secret_name=f"{prefix}/aurora-credentials",
        )

        # ── Aurora cluster ────────────────────────────────────────────────────
        # engine_version: 5.7.mysql_aurora.2.x.y family is wire-compatible with
        # on-premise MySQL 5.7 — the same protocol version, charset defaults, and
        # SQL mode as the source being migrated.
        cluster = rds.DatabaseCluster(
            self, "AuroraCluster",
            cluster_identifier=f"{prefix}-cluster",
            engine=engine,
            credentials=db_credentials,
            default_database_name=db_name,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            security_groups=[aurora_sg],
            parameter_group=cluster_pg,
            backup=rds.BackupProps(
                # Backup window in UTC — low-traffic hours.
                # backup_days controls how far back point-in-time restore reaches.
                retention=Duration.days(backup_days),
                preferred_window="03:00-04:00",
            ),
            # Weekly maintenance window outside business hours (UTC).
            # Aurora applies minor version patches and parameter changes here.
            preferred_maintenance_window="sun:05:00-sun:06:00",
            # Encryption at rest using the default AWS-managed KMS key for RDS.
            storage_encrypted=True,
            deletion_protection=deletion_protection,
            # SNAPSHOT: CloudFormation retains a final snapshot before stack deletion.
            # DESTROY:  stack deletion drops the cluster immediately (dev default).
            removal_policy=(
                RemovalPolicy.SNAPSHOT if deletion_protection else RemovalPolicy.DESTROY
            ),
            # Ship Aurora audit, error, general, and slow-query logs to CloudWatch.
            # Retention is capped at one month to limit log storage costs.
            cloudwatch_logs_exports=["audit", "error", "general", "slowquery"],
            cloudwatch_logs_retention=logs.RetentionDays.ONE_MONTH,
            # Writer instance — always created; receives all DML during migration.
            writer=rds.ClusterInstance.provisioned(
                "writer",
                instance_identifier=f"{prefix}-writer",
                instance_type=ec2.InstanceType(f"db.{instance_class_str}"),
                parameter_group=instance_pg,
                # Performance Insights gives query-level visibility into Aurora wait
                # events — useful for diagnosing bulk-load bottlenecks.
                enable_performance_insights=True,
                # Pin minor version to prevent unexpected restarts during the
                # migration window from automatic minor-version upgrades.
                auto_minor_version_upgrade=False,
            ),
            # Reader instances: range(instance_count - 1) produces an empty list when
            # instance_count = 1 (writer-only / dev mode) and one reader when = 2 (HA).
            # For production, set cluster_instance_count = 2 in cdk.json.
            readers=[
                rds.ClusterInstance.provisioned(
                    f"reader-{i + 1}",
                    instance_identifier=f"{prefix}-reader-{i + 1}",
                    instance_type=ec2.InstanceType(f"db.{instance_class_str}"),
                    parameter_group=instance_pg,
                    enable_performance_insights=True,
                    auto_minor_version_upgrade=False,
                )
                for i in range(instance_count - 1)
            ],
        )

        # ── CfnOutputs ────────────────────────────────────────────────────────
        # Visible in the CDK deploy output and stored in CloudFormation exports.

        CfnOutput(self, "ClusterEndpoint",
            value=cluster.cluster_endpoint.hostname,
            description="Writer endpoint — set as AURORA_HOST in migration scripts",
            export_name=f"{prefix}-cluster-endpoint",
        )
        CfnOutput(self, "ReaderEndpoint",
            value=cluster.cluster_read_endpoint.hostname,
            description="Reader endpoint for read-only traffic after migration",
        )
        CfnOutput(self, "ClusterPort",
            value=str(cluster.cluster_endpoint.port),
            description="Aurora cluster port (always 3306)",
        )
        CfnOutput(self, "DatabaseName",
            value=db_name,
            description="Initial database name — set as AURORA_DB in migration scripts",
        )
        CfnOutput(self, "MasterUsername",
            value=db_username,
            description="Master username — set as AURORA_USER in migration scripts",
        )

        # cluster.secret is set when using from_generated_secret credentials
        if cluster.secret:
            CfnOutput(self, "SecretArn",
                value=cluster.secret.secret_arn,
                description="Secrets Manager ARN holding the auto-generated Aurora credentials",
            )
            retrieve_cmd = (
                f"aws secretsmanager get-secret-value "
                f"--secret-id {prefix}/aurora-credentials "
                f"--query SecretString --output text "
                f"| python3 -c \"import json,sys; print(json.load(sys.stdin)['password'])\""
            )
            CfnOutput(self, "RetrievePasswordCommand",
                value=retrieve_cmd,
                description="Command to retrieve TGT_PASSWORD from Secrets Manager",
            )

        CfnOutput(self, "MigrationEnvVars",
            value=(
                f"export AURORA_HOST={cluster.cluster_endpoint.hostname} "
                f"AURORA_PORT=3306 "
                f"AURORA_USER={db_username} "
                f"AURORA_DB={db_name} "
                f"SRC_PASSWORD=migpw"
            ),
            description=(
                "Partial env-var block for run_poc_aurora.sh. "
                "Append TGT_PASSWORD retrieved from Secrets Manager."
            ),
        )
