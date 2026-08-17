# ──────────────────────────────────────────────────────────────────────────────
# Terraform version constraints, provider pins, and Terraform Cloud backend
# for the "prod" environment.
#
# Each environment directory has its OWN Terraform Cloud workspace, so state is
# isolated per environment — applying here can never overwrite dev/qa/staging.
#
# ACCOUNT: prod deploys into a SEPARATE AWS account from dev/qa/staging
# (mirroring infra/cdk's AWS_ACCOUNT_PROD model). Terraform's AWS provider has
# no account-resolution logic of its own — it simply uses whatever credentials
# are active in your shell (AWS_PROFILE, AWS_ACCESS_KEY_ID, SSO, ...). Always
# run `aws sts get-caller-identity` before `terraform apply` here to confirm
# the active session is actually scoped to the prod account, e.g.:
#     export AWS_PROFILE=marketo-prod
#     aws sts get-caller-identity
#     terraform -chdir=infra/terraform/environments/prod plan
#
# Before running:
#
#   1. Sign up / log in at https://app.terraform.io
#   2. Replace REPLACE_WITH_YOUR_TF_CLOUD_ORG below with your organisation name,
#      OR set the environment variable TF_CLOUD_ORGANIZATION instead.
#   3. The workspace "marketo-aurora-mysql-prod" is created automatically on
#      first apply.
#   4. Authenticate by running:
#        terraform login
#      or by setting the environment variable:
#        TF_TOKEN_app_terraform_io=<your-token>
# ──────────────────────────────────────────────────────────────────────────────

terraform {
  cloud {
    organization = "REPLACE_WITH_YOUR_TF_CLOUD_ORG"

    workspaces {
      name = "marketo-aurora-mysql-prod"
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
      Environment = "prod"
      ManagedBy   = "terraform"
      Purpose     = "mysql-to-aurora-migration"
    }
  }
}
