output "cluster_endpoint" {
  description = "Writer endpoint for the Aurora cluster. Use as AURORA_HOST in migration scripts."
  value       = module.aurora.cluster_endpoint
}

output "reader_endpoint" {
  description = "Reader endpoint for the Aurora cluster (for read-only traffic after migration)."
  value       = module.aurora.reader_endpoint
}

output "cluster_port" {
  description = "Port the Aurora cluster listens on (always 3306)."
  value       = module.aurora.cluster_port
}

output "database_name" {
  description = "Name of the initial database created inside the Aurora cluster."
  value       = module.aurora.database_name
}

output "master_username" {
  description = "Master username for the Aurora cluster. Use as AURORA_USER in migration scripts."
  value       = module.aurora.master_username
}

output "cluster_id" {
  description = "Aurora cluster identifier."
  value       = module.aurora.cluster_id
}

output "vpc_id" {
  description = "VPC ID where the Aurora cluster was deployed."
  value       = module.aurora.vpc_id
}

output "subnet_ids" {
  description = "Subnet IDs used by the Aurora DB subnet group."
  value       = module.aurora.subnet_ids
}

output "security_group_id" {
  description = "Security group ID attached to the Aurora cluster (add inbound rules here for new hosts)."
  value       = module.aurora.security_group_id
}

output "secret_arn" {
  description = "ARN of the Secrets Manager secret containing the Aurora master credentials."
  value       = module.aurora.secret_arn
}

output "secret_name" {
  description = "Name of the Secrets Manager secret (usable with --secret-id in the AWS CLI)."
  value       = module.aurora.secret_name
}

output "retrieve_tgt_password_command" {
  description = "Run this command to export TGT_PASSWORD from Secrets Manager before migrating."
  value       = module.aurora.retrieve_tgt_password_command
}

output "migration_env_vars" {
  description = "Environment variable block for run_poc_aurora.sh. Append TGT_PASSWORD (see above)."
  value       = module.aurora.migration_env_vars
}
