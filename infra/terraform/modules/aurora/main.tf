# ──────────────────────────────────────────────────────────────────────────────
# Locals: resolve VPC / subnet references regardless of create_vpc mode.
# ──────────────────────────────────────────────────────────────────────────────

locals {
  # Consistent name prefix for every resource in this stack
  prefix = "${var.project_name}-${var.environment}"

  # Use the newly created VPC when create_vpc = true; otherwise reference existing
  vpc_id     = var.create_vpc ? aws_vpc.this[0].id : var.existing_vpc_id
  subnet_ids = var.create_vpc ? [for s in aws_subnet.private : s.id] : var.existing_subnet_ids

  # When no allowed CIDRs are given and a VPC is being created, default to the
  # VPC CIDR so that hosts inside the VPC can reach Aurora immediately.
  effective_allowed_cidrs = (
    length(var.allowed_cidr_blocks) > 0
    ? var.allowed_cidr_blocks
    : (var.create_vpc ? [var.vpc_cidr] : [])
  )
}


# ── VPC (conditional) ─────────────────────────────────────────────────────────
# Created only when create_vpc = true.
# Two private (non-public) subnets are spread across the two configured AZs
# to satisfy the Aurora DB subnet group requirement of multi-AZ coverage.
# No internet gateway or NAT gateway is included — Aurora is only reachable
# from hosts that share the VPC (EC2, VPN, or Direct Connect).

resource "aws_vpc" "this" {
  count = var.create_vpc ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.prefix}-vpc" }
}

resource "aws_subnet" "private" {
  # One subnet per availability zone, keyed by AZ name for deterministic names
  for_each = var.create_vpc ? toset(var.availability_zones) : toset([])

  vpc_id            = aws_vpc.this[0].id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, index(var.availability_zones, each.key))
  availability_zone = each.key

  tags = { Name = "${local.prefix}-private-${each.key}" }
}


# ── Security group ────────────────────────────────────────────────────────────
# Allows port 3306 inbound from every CIDR in allowed_cidr_blocks (or the VPC
# CIDR when left empty).  The migration host must be within one of these CIDRs.

resource "aws_security_group" "aurora" {
  name        = "${local.prefix}-aurora-sg"
  description = "Allow MySQL/Aurora ingress from migration hosts on port 3306"
  vpc_id      = local.vpc_id

  tags = { Name = "${local.prefix}-aurora-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "mysql" {
  # One ingress rule per CIDR block
  for_each = toset(local.effective_allowed_cidrs)

  security_group_id = aws_security_group.aurora.id
  description       = "MySQL from ${each.key}"
  from_port         = 3306
  to_port           = 3306
  ip_protocol       = "tcp"
  cidr_ipv4         = each.key
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.aurora.id
  description       = "Allow all outbound traffic"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}


# ── DB subnet group ───────────────────────────────────────────────────────────
# Aurora requires subnets in at least two AZs.

resource "aws_db_subnet_group" "aurora" {
  name        = "${local.prefix}-aurora-subnet-group"
  description = "Subnet group for ${local.prefix} Aurora cluster"
  subnet_ids  = local.subnet_ids

  tags = { Name = "${local.prefix}-aurora-subnet-group" }
}


# ── Cluster parameter group ───────────────────────────────────────────────────
# Applies to the Aurora cluster as a whole (not individual instances).
#
# binlog_format = ROW enables row-based binary logging on Aurora so it can
# serve as a CDC replication source in future failback scenarios.
# binlog_checksum = NONE matches the on-premise source default and is required
# for mysql-replication library compatibility.

resource "aws_rds_cluster_parameter_group" "aurora" {
  name        = "${local.prefix}-cluster-pg"
  family      = "aurora-mysql5.7"
  description = "Cluster PG for ${local.prefix}: binlog ROW, checksum NONE"

  parameter {
    name  = "binlog_format"
    value = "ROW"
    # "pending-reboot" is required for static parameters on Aurora;
    # a cluster reboot is needed for this change to take effect.
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "binlog_checksum"
    value = "NONE"
    # Same static-parameter constraint — takes effect after a cluster reboot.
    apply_method = "pending-reboot"
  }

  tags = { Name = "${local.prefix}-cluster-pg" }
}


# ── DB parameter group ────────────────────────────────────────────────────────
# Applies per Aurora instance.  Kept at defaults for MySQL 5.7 compatibility.

resource "aws_db_parameter_group" "aurora" {
  name        = "${local.prefix}-db-pg"
  family      = "aurora-mysql5.7"
  description = "Instance PG for ${local.prefix} Aurora MySQL 5.7"

  tags = { Name = "${local.prefix}-db-pg" }
}


# ── Credentials ───────────────────────────────────────────────────────────────
# A random password is generated by Terraform and stored in Secrets Manager.
# It is never embedded in tfvars or state plaintext — only in the encrypted
# Secrets Manager secret.  Retrieve it with the `retrieve_tgt_password_command`
# output after running terraform apply.

resource "random_password" "db" {
  length  = 20
  special = true
  # Exclude characters that cause shell-quoting issues in env-var exports
  override_special = "!#%*()-_=+[]{}:<>?"
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${local.prefix}/aurora-credentials"
  description             = "Auto-generated master credentials for the ${local.prefix} Aurora cluster"
  recovery_window_in_days = 0 # Set to 7 or higher for production environments

  tags = { Name = "${local.prefix}/aurora-credentials" }
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id

  # Store the full connection bundle so operators can use it directly
  secret_string = jsonencode({
    username = var.db_master_username
    password = random_password.db.result
    host     = aws_rds_cluster.aurora.endpoint
    reader   = aws_rds_cluster.aurora.reader_endpoint
    dbname   = var.db_name
    port     = 3306
    engine   = "aurora-mysql"
  })
}


# ── Enhanced monitoring IAM role (conditional) ────────────────────────────────
# Required by Aurora enhanced monitoring to publish OS-level metrics to CloudWatch.

resource "aws_iam_role" "rds_monitoring" {
  count = var.enable_enhanced_monitoring ? 1 : 0

  name               = "${local.prefix}-rds-monitoring-role"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume[0].json

  tags = { Name = "${local.prefix}-rds-monitoring-role" }
}

data "aws_iam_policy_document" "rds_monitoring_assume" {
  count = var.enable_enhanced_monitoring ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  count = var.enable_enhanced_monitoring ? 1 : 0

  role       = aws_iam_role.rds_monitoring[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}


# ── Aurora cluster ────────────────────────────────────────────────────────────
# Aurora MySQL 2.x is wire-compatible with MySQL 5.7.  The migration scripts
# connect to the writer endpoint using the standard MySQL protocol via PyMySQL
# and the host-installed mysql CLI client.

resource "aws_rds_cluster" "aurora" {
  cluster_identifier = "${local.prefix}-cluster"

  engine         = "aurora-mysql"
  engine_version = var.db_engine_version

  database_name   = var.db_name
  master_username = var.db_master_username
  master_password = random_password.db.result

  db_subnet_group_name            = aws_db_subnet_group.aurora.name
  vpc_security_group_ids          = [aws_security_group.aurora.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.aurora.name

  # Encryption at rest using the default AWS-managed KMS key for RDS.
  # Required for compliance; has no performance impact on Aurora.
  storage_encrypted = true

  # Backup window in UTC — chosen to fall in a low-traffic period.
  # backup_retention_period controls how far back point-in-time restore can reach.
  backup_retention_period = var.backup_retention_period
  preferred_backup_window = "03:00-04:00"

  # Weekly maintenance window (minor version patches, parameter changes, etc.).
  # Kept outside business hours; widened to 1 hour to give Aurora enough time.
  preferred_maintenance_window = "sun:05:00-sun:06:00"

  # Export Aurora logs to CloudWatch for auditing, error diagnosis, and slow-query analysis.
  # "audit" requires the audit plugin; "general" can be noisy — disable in prod if cost is a concern.
  enabled_cloudwatch_logs_exports = ["audit", "error", "general", "slowquery"]

  # When deletion_protection = true: Terraform will refuse to destroy the cluster
  # and will take a final snapshot before any delete is allowed.
  # When deletion_protection = false (dev default): the cluster is destroyed without a snapshot.
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = !var.deletion_protection
  final_snapshot_identifier = var.deletion_protection ? "${local.prefix}-final-snapshot" : null

  # Apply parameter and configuration changes immediately rather than waiting for
  # the next maintenance window.  Acceptable during initial provisioning; set to
  # false in production to avoid unexpected restarts outside the maintenance window.
  apply_immediately = true

  tags = { Name = "${local.prefix}-cluster" }

  # Ensure the DB subnet group and parameter group exist before the cluster
  depends_on = [
    aws_db_subnet_group.aurora,
    aws_rds_cluster_parameter_group.aurora,
  ]
}


# ── Aurora cluster instances ──────────────────────────────────────────────────
# Instance 1 is the writer; instances 2+ are read replicas.
# For a one-way migration POC: cluster_instance_count = 1 is sufficient.
# For production HA: set cluster_instance_count = 2.

resource "aws_rds_cluster_instance" "aurora" {
  count = var.cluster_instance_count

  identifier         = "${local.prefix}-instance-${count.index + 1}"
  cluster_identifier = aws_rds_cluster.aurora.id

  engine         = aws_rds_cluster.aurora.engine
  engine_version = aws_rds_cluster.aurora.engine_version
  instance_class = var.db_instance_class

  db_parameter_group_name = aws_db_parameter_group.aurora.name
  db_subnet_group_name    = aws_db_subnet_group.aurora.name

  # monitoring_role_arn / monitoring_interval: wire up enhanced monitoring only when
  # the feature is enabled; a zero interval disables it without requiring a null ARN.
  monitoring_role_arn = var.enable_enhanced_monitoring ? aws_iam_role.rds_monitoring[0].arn : null
  monitoring_interval = var.enable_enhanced_monitoring ? var.monitoring_interval : 0

  # Performance Insights provides query-level visibility into Aurora wait events
  # and top SQL — useful during and after the migration for capacity planning.
  performance_insights_enabled = true

  # Pinning auto_minor_version_upgrade = false ensures version stability during
  # the migration window — avoids unexpected restarts from minor version updates.
  auto_minor_version_upgrade = false

  # Same rationale as the cluster — apply changes immediately during provisioning.
  apply_immediately = true

  tags = { Name = "${local.prefix}-instance-${count.index + 1}" }
}
