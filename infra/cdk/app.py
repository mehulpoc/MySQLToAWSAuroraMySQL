#!/usr/bin/env python3
"""
CDK application entry point.

Usage:
    # Bootstrap the CDK toolkit into your AWS account (one-time):
    cdk bootstrap

    # Preview what will be created:
    cdk diff

    # Deploy the Aurora cluster:
    cdk deploy

    # Override context values without editing cdk.json:
    cdk deploy --context environment=prod --context cluster_instance_count=2

    # Tear down (only when deletion_protection=false):
    cdk destroy
"""
from __future__ import annotations

import os
import aws_cdk as cdk
from aurora_stack import AuroraStack

# One env var per environment lets CI/CD supply account IDs without committing
# them to cdk.json. The env var always takes precedence over the "account"
# value (if any) under context.environments.<name> in cdk.json.
ACCOUNT_ENV_VARS = {
    "dev":     "AWS_ACCOUNT_DEV",
    "qa":      "AWS_ACCOUNT_QA",
    "staging": "AWS_ACCOUNT_STAGING",
    "prod":    "AWS_ACCOUNT_PROD",
}


def main() -> None:
    """
    Instantiate the CDK app, resolve the target AWS environment from the shell,
    read project + per-environment context from cdk.json (or --context CLI
    flags), and synthesise the CloudFormation template.

    Environment selection:
        cdk deploy --context environment=qa
        cdk deploy --context environment=prod

    dev/qa/staging typically share one AWS account; prod deploys into a
    separate account. Account/region come from (in priority order):
        1. AWS_ACCOUNT_<ENV> env var (e.g. AWS_ACCOUNT_PROD)
        2. "account" key under context.environments.<environment> in cdk.json
        3. CDK_DEFAULT_ACCOUNT / AWS_ACCOUNT_ID env vars (whatever profile
           'cdk deploy' is currently running under)
    """
    app = cdk.App()

    # Pull project-level context values defined in cdk.json.
    # Any of these can be overridden at deploy time with --context key=value.
    project_name = app.node.try_get_context("project_name") or "marketo-migration"
    environment  = app.node.try_get_context("environment")  or "dev"

    # Per-environment overrides (instance size, HA count, deletion protection,
    # networking, account/region, ...) declared under context.environments in
    # cdk.json. Falls back to {} for an unrecognised environment name so the
    # stack still synthesises using its own hardcoded defaults.
    environments = app.node.try_get_context("environments") or {}
    env_config = environments.get(environment, {})

    account_env_var = ACCOUNT_ENV_VARS.get(environment)
    account = (
        (os.environ.get(account_env_var) if account_env_var else None)
        or env_config.get("account")
        or os.environ.get("CDK_DEFAULT_ACCOUNT")
        or os.environ.get("AWS_ACCOUNT_ID")
    )
    region = (
        os.environ.get("CDK_DEFAULT_REGION")
        or env_config.get("region")
        or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    env = cdk.Environment(account=account or None, region=region)

    # Stack ID doubles as the CloudFormation stack name
    stack_id = f"{project_name}-{environment}-aurora"

    AuroraStack(
        app,
        stack_id,
        project_name=project_name,
        environment=environment,
        config=env_config,
        env=env,
        description=(
            f"Aurora MySQL 2.x (5.7-compatible) migration target "
            f"for {project_name} ({environment})"
        ),
    )

    app.synth()


if __name__ == "__main__":
    main()
