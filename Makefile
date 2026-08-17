.PHONY: up down seed reset install test poc aurora-poc cdk-diff cdk-deploy cdk-destroy tf-preflight tf-init tf-plan tf-apply tf-destroy tf-iam-bootstrap

up:
	docker compose -f docker/compose.yaml up -d

down:
	docker compose -f docker/compose.yaml down

seed:
	bash scripts/bootstrap.sh

reset:
	docker compose -f docker/compose.yaml down -v
	docker compose -f docker/compose.yaml up -d
	bash scripts/bootstrap.sh

install:
	pip install -r requirements.txt

test:
	pytest -x -v

poc:
	SRC_PASSWORD=migpw TGT_PASSWORD=tgtpw bash scripts/run_poc.sh

aurora-poc:
	@: "$${AURORA_HOST:?Set AURORA_HOST to your Aurora cluster endpoint}"
	@: "$${AURORA_USER:?Set AURORA_USER to your Aurora master/superuser account}"
	@: "$${TGT_PASSWORD:?Set TGT_PASSWORD to your Aurora user password}"
	SRC_PASSWORD=$${SRC_PASSWORD:-migpw} bash scripts/run_poc_aurora.sh

# Usage: make cdk-diff ENV=qa | make cdk-deploy ENV=prod | make cdk-destroy ENV=dev
ENV ?= dev

cdk-diff:
	cd infra/cdk && cdk diff --context environment=$(ENV)

cdk-deploy:
	cd infra/cdk && cdk deploy --context environment=$(ENV)

cdk-destroy:
	cd infra/cdk && cdk destroy --context environment=$(ENV)

# Usage: make tf-init ENV=qa | make tf-plan ENV=qa | make tf-apply ENV=prod | make tf-destroy ENV=dev
# ENV selects infra/terraform/environments/<ENV> — each is its own root module
# with its own Terraform Cloud workspace/state, so envs never collide.
# Pass extra vars with TF_ARGS, e.g.:
#   make tf-apply ENV=prod TF_ARGS='-var="allowed_cidr_blocks=[\"10.0.0.0/8\"]"'
TF_DIR := infra/terraform/environments
TF_ENV_DIR := $(TF_DIR)/$(ENV)

tf-preflight:
	@test -d "$(TF_ENV_DIR)" || (echo "Terraform environment '$(ENV)' not found under $(TF_DIR)." && exit 1)
	@terraform -chdir="$(TF_ENV_DIR)" init -input=false >/tmp/tf-init-$(ENV).log 2>&1 || \
		(status=$$?; \
		if rg -q 'lookup app\.terraform\.io: no such host|Failed to request discovery document' /tmp/tf-init-$(ENV).log; then \
			echo "Terraform Cloud is required for ENV=$(ENV), but app.terraform.io is not reachable from this shell."; \
			echo "Check network/DNS access, then authenticate with 'terraform login' or set TF_TOKEN_app_terraform_io."; \
		else \
			cat /tmp/tf-init-$(ENV).log; \
		fi; \
		exit $$status)

tf-init: tf-preflight

tf-plan: tf-preflight
	cd $(TF_ENV_DIR) && terraform plan $(TF_ARGS)

tf-apply: tf-preflight
	cd $(TF_ENV_DIR) && terraform apply $(TF_ARGS)

tf-destroy: tf-preflight
	cd $(TF_ENV_DIR) && terraform destroy $(TF_ARGS)

# Usage: make tf-iam-bootstrap ENV=qa
# Creates/updates the AWS IAM OIDC provider, role, and policy that let the
# ENV's HCP Terraform workspace authenticate to AWS via dynamic credentials
# (no static keys). Idempotent — safe to re-run. Requires AWS credentials
# active in the shell (scoped to the right account for ENV) and a prior
# 'terraform login'. The workspace must already exist in HCP Terraform
# (run 'make tf-init ENV=$(ENV)' first if it doesn't).
tf-iam-bootstrap:
	ENV=$(ENV) bash scripts/setup_tf_oidc_role.sh
