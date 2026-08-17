# ── Environment / naming ────────────────────────────────────────────────────────

variable "environment" {
  description = "Deployment environment (dev, qa, staging, prod). Used in resource names and tags."
  type        = string

  validation {
    condition     = contains(["dev", "qa", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, qa, staging, prod."
  }
}

variable "project_name" {
  description = "Project name used as a prefix for all resource names and Secrets Manager paths."
  type        = string
  default     = "marketo-migration"
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "create_vpc" {
  description = <<-EOT
    When true  — a dedicated VPC with two private subnets across two AZs is created.
    When false — supply existing_vpc_id and existing_subnet_ids instead.
  EOT
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "CIDR block for the new VPC. Only used when create_vpc = true."
  type        = string
  default     = "10.10.0.0/16"
}

variable "availability_zones" {
  description = <<-EOT
    Exactly two AZs for the Aurora DB subnet group.
    Only used when create_vpc = true. Must be valid AZs in the target region.
  EOT
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]

  validation {
    condition     = length(var.availability_zones) == 2
    error_message = "Exactly two availability zones are required for the DB subnet group."
  }
}

variable "existing_vpc_id" {
  description = "ID of an existing VPC. Required when create_vpc = false."
  type        = string
  default     = ""
}

variable "existing_subnet_ids" {
  description = <<-EOT
    List of private subnet IDs in the existing VPC (at least 2 in different AZs).
    Required when create_vpc = false.
  EOT
  type        = list(string)
  default     = []
}

variable "allowed_cidr_blocks" {
  description = <<-EOT
    CIDR blocks permitted to connect to Aurora on port 3306.
    Examples:
      - VPN tunnel CIDR for on-premise migration hosts with Direct Connect / VPN.
      - EC2 subnet CIDR if the migration host runs inside the same VPC.
    Defaults to the VPC CIDR when create_vpc = true and left empty.
  EOT
  type        = list(string)
  default     = []
}

# ── Database ──────────────────────────────────────────────────────────────────

variable "db_name" {
  description = "Name of the initial database to create inside the Aurora cluster."
  type        = string
  default     = "marketo_tgt"
}

variable "db_master_username" {
  description = "Master username for the Aurora cluster."
  type        = string
  default     = "admin"
}

variable "db_engine_version" {
  description = <<-EOT
    Aurora MySQL engine version. The 5.7.mysql_aurora.2.x.y family is compatible
    with on-premise MySQL 5.7. Use Aurora MySQL 3.x (8.0-compatible) for newer sources.
  EOT
  type        = string
  default     = "5.7.mysql_aurora.2.12.1"
}

variable "db_instance_class" {
  description = "Aurora instance class, e.g. db.r6g.large."
  type        = string
  default     = "db.r6g.large"
}

variable "cluster_instance_count" {
  description = <<-EOT
    Number of Aurora DB instances.
      1 — writer only (suitable for dev / qa / one-way migration).
      2 — writer + one reader replica (recommended for staging / production).
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.cluster_instance_count >= 1
    error_message = "At least one Aurora instance is required."
  }
}

variable "backup_retention_period" {
  description = "Number of days to retain automated backups (1-35)."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Prevent the Aurora cluster from being deleted via the API or console. Set true for staging/production."
  type        = bool
  default     = false
}

variable "enable_enhanced_monitoring" {
  description = "Enable CloudWatch enhanced monitoring for the Aurora instances."
  type        = bool
  default     = true
}

variable "monitoring_interval" {
  description = "Enhanced monitoring interval in seconds (1, 5, 10, 15, 30, or 60). 0 = disabled."
  type        = number
  default     = 60
}
