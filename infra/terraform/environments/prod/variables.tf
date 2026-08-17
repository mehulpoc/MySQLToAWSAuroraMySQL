# Pass-through variables an operator can still override per-deploy with
# -var / -var-file without editing main.tf. Sizing that's meant to stay fixed
# per environment (instance class, HA count, backup retention, deletion
# protection, VPC CIDR) is hardcoded in main.tf instead — see its header comment.

variable "aws_region" {
  description = "AWS region where the Aurora cluster will be created."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used as a prefix for all resource names and Secrets Manager paths."
  type        = string
  default     = "marketo-migration"
}

variable "create_vpc" {
  description = "When true, a dedicated VPC is created. When false, supply existing_vpc_id / existing_subnet_ids."
  type        = bool
  default     = true
}

variable "existing_vpc_id" {
  description = "ID of an existing VPC. Required when create_vpc = false."
  type        = string
  default     = ""
}

variable "existing_subnet_ids" {
  description = "Private subnet IDs in the existing VPC (at least 2 in different AZs). Required when create_vpc = false."
  type        = list(string)
  default     = []
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks permitted to connect to Aurora on port 3306. Defaults to the VPC CIDR when left empty."
  type        = list(string)
  default     = []
}

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
  description = "Aurora MySQL engine version. 5.7.mysql_aurora.2.x.y is compatible with on-premise MySQL 5.7."
  type        = string
  default     = "5.7.mysql_aurora.2.12.1"
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
