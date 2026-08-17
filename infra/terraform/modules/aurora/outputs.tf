# ── Connection endpoints ───────────────────────────────────────────────────────

output "cluster_endpoint" {
  description = "Writer endpoint for the Aurora cluster. Use as AURORA_HOST in migration scripts."
  value       = aws_rds_cluster.aurora.endpoint
}

output "reader_endpoint" {
  description = "Reader endpoint for the Aurora cluster (for read-only traffic after migration)."
  value       = aws_rds_cluster.aurora.reader_endpoint
}

output "cluster_port" {
  description = "Port the Aurora cluster listens on (always 3306)."
  value       = aws_rds_cluster.aurora.port
}

output "database_name" {
  description = "Name of the initial database created inside the Aurora cluster."
  value       = aws_rds_cluster.aurora.database_name
}

output "master_username" {
  description = "Master username for the Aurora cluster. Use as AURORA_USER in migration scripts."
  value       = aws_rds_cluster.aurora.master_username
}

output "cluster_id" {
  description = "Aurora cluster identifier."
  value       = aws_rds_cluster.aurora.cluster_identifier
}


# ── Networking ────────────────────────────────────────────────────────────────

output "vpc_id" {
  description = "VPC ID where the Aurora cluster was deployed."
  value       = local.vpc_id
}

output "subnet_ids" {
  description = "Subnet IDs used by the Aurora DB subnet group."
  value       = local.subnet_ids
}

output "security_group_id" {
  description = "Security group ID attached to the Aurora cluster (add inbound rules here for new hosts)."
  value       = aws_security_group.aurora.id
}


# ── Secrets Manager ───────────────────────────────────────────────────────────

output "secret_arn" {
  description = "ARN of the Secrets Manager secret containing the Aurora master credentials."
  value       = aws_secretsmanager_secret.db.arn
}

output "secret_name" {
  description = "Name of the Secrets Manager secret (usable with --secret-id in the AWS CLI)."
  value       = aws_secretsmanager_secret.db.name
}


# ── Migration script helpers ───────────────────────────────────────────────────
# Copy-paste these commands to configure and run the migration scripts.

output "retrieve_tgt_password_command" {
  description = "Run this command to export TGT_PASSWORD from Secrets Manager before migrating."
  value       = <<-EOT
    export TGT_PASSWORD=$(aws secretsmanager get-secret-value \
      --secret-id "${aws_secretsmanager_secret.db.name}" \
      --query SecretString --output text \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])")
  EOT
}

output "migration_env_vars" {
  description = "Environment variable block for run_poc_aurora.sh. Append TGT_PASSWORD (see above)."
  value       = <<-EOT
    export AURORA_HOST="${aws_rds_cluster.aurora.endpoint}"
    export AURORA_PORT="3306"
    export AURORA_USER="${aws_rds_cluster.aurora.master_username}"
    export AURORA_DB="${aws_rds_cluster.aurora.database_name}"
    export SRC_PASSWORD="migpw"
    # Then run: eval "$(terraform output -raw retrieve_tgt_password_command)"
    # Finally:  bash scripts/run_poc_aurora.sh
  EOT
}
