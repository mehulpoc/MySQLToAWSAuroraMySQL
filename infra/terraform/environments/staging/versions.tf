# ──────────────────────────────────────────────────────────────────────────────
# Terraform version constraints, provider pins, and Terraform Cloud backend
# for the "staging" environment.
#
# Each environment directory has its OWN Terraform Cloud workspace, so state is
# isolated per environment — applying here can never overwrite dev/qa/prod.
#
# Before running:
#
#   1. Sign up / log in at https://app.terraform.io
#   2. Replace REPLACE_WITH_YOUR_TF_CLOUD_ORG below with your organisation name,
#      OR set the environment variable TF_CLOUD_ORGANIZATION instead.
#   3. The workspace "marketo-aurora-mysql-staging" is created automatically on
#      first apply.
#   4. Authenticate by running:
#        terraform login
#      or by setting the environment variable:
#        TF_TOKEN_app_terraform_io=<your-token>
#   5. AWS credentials come from your environment (AWS_PROFILE,
#      AWS_ACCESS_KEY_ID, SSO, ...) — there is no account resolution baked
#      into this config. dev/qa/staging share one AWS account, prod uses a
#      separate account (see ../prod/versions.tf); make sure the active
#      credentials are scoped to the right account before applying.
# ──────────────────────────────────────────────────────────────────────────────

terraform {
  cloud {
    organization = "REPLACE_WITH_YOUR_TF_CLOUD_ORG"

    workspaces {
      name = "marketo-aurora-mysql-staging"
    }
  }

  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# ── AWS provider ──────────────────────────────────────────────────────────────
# Default tags applied to every resource created by this configuration.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = "staging"
      ManagedBy   = "terraform"
      Purpose     = "mysql-to-aurora-migration"
    }
  }
}
