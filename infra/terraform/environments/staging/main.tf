# "staging" environment — HA (writer + reader), deletion-protected, still in
# the shared dev/qa/staging AWS account. A production-shaped rehearsal
# environment.
#
# Per-environment sizing is hardcoded here rather than passed on the CLI — see
# ../dev/main.tf for the rationale. Keep in sync with the "staging" block
# under context.environments in infra/cdk/cdk.json.

module "aurora" {
  source = "../../modules/aurora"

  project_name = var.project_name
  environment  = "staging"

  create_vpc          = var.create_vpc
  vpc_cidr            = "10.30.0.0/16"
  availability_zones  = ["us-east-1a", "us-east-1b"]
  existing_vpc_id     = var.existing_vpc_id
  existing_subnet_ids = var.existing_subnet_ids
  allowed_cidr_blocks = var.allowed_cidr_blocks

  db_name            = var.db_name
  db_master_username = var.db_master_username
  db_engine_version  = var.db_engine_version
  db_instance_class  = "db.r6g.large"

  cluster_instance_count  = 2
  backup_retention_period = 7
  deletion_protection     = true

  enable_enhanced_monitoring = var.enable_enhanced_monitoring
  monitoring_interval        = var.monitoring_interval
}
