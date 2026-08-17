# "qa" environment — same shared account and cost profile as dev, but its own
# VPC and data so QA runs don't collide with dev testing.
#
# Per-environment sizing is hardcoded here rather than passed on the CLI — see
# ../dev/main.tf for the rationale. Keep in sync with the "qa" block under
# context.environments in infra/cdk/cdk.json.

module "aurora" {
  source = "../../modules/aurora"

  project_name = var.project_name
  environment  = "qa"

  create_vpc          = var.create_vpc
  vpc_cidr            = "10.20.0.0/16"
  availability_zones  = ["us-east-1a", "us-east-1b"]
  existing_vpc_id     = var.existing_vpc_id
  existing_subnet_ids = var.existing_subnet_ids
  allowed_cidr_blocks = var.allowed_cidr_blocks

  db_name            = var.db_name
  db_master_username = var.db_master_username
  db_engine_version  = var.db_engine_version
  db_instance_class  = "db.t4g.medium"

  cluster_instance_count  = 1
  backup_retention_period = 3
  deletion_protection     = false

  enable_enhanced_monitoring = var.enable_enhanced_monitoring
  monitoring_interval        = var.monitoring_interval
}
