#!/usr/bin/env bash
# setup_tf_oidc_role.sh — create/update the AWS IAM OIDC provider, role, and
# policy that let an HCP Terraform workspace authenticate to AWS via dynamic
# credentials (no long-lived access keys stored in HCP Terraform).
#
# Idempotent: safe to re-run. Reuses the OIDC provider if one already exists
# for app.terraform.io, updates the role's trust policy and the managed
# policy's default version in place, and upserts (rather than duplicates)
# the two HCP Terraform workspace variables.
#
# Required:
#   - AWS credentials active in the shell (AWS_PROFILE / AWS_ACCESS_KEY_ID /
#     SSO, ...), scoped to the account this environment deploys into. Run
#     `aws sts get-caller-identity` yourself first if unsure which account
#     is active — this script does not switch profiles for you.
#   - `terraform login` already run (token read from
#     ~/.terraform.d/credentials.tfrc.json) so the script can call the HCP
#     Terraform API.
#   - The target workspace must already exist in HCP Terraform (created by
#     `terraform init` / `make tf-init ENV=<env>` against the `cloud` block
#     in infra/terraform/environments/<env>/versions.tf).
#
# Usage:
#   ENV=dev bash scripts/setup_tf_oidc_role.sh
#   make tf-iam-bootstrap ENV=qa
#
# Optional overrides:
#   TF_CLOUD_ORGANIZATION  — override the org read from versions.tf
#   TF_WORKSPACE_NAME      — override the workspace name (default: marketo-aurora-mysql-<ENV>)
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV="${ENV:-dev}"
TF_ENV_DIR="${REPO_ROOT}/infra/terraform/environments/${ENV}"
VERSIONS_TF="${TF_ENV_DIR}/versions.tf"

TFC_HOSTNAME="app.terraform.io"
TFC_CREDENTIALS_FILE="${HOME}/.terraform.d/credentials.tfrc.json"
TFC_AUDIENCE="aws.workload.identity"

ROLE_NAME="hcp-terraform-marketo-aurora-${ENV}"
POLICY_NAME="hcp-terraform-marketo-aurora-${ENV}-policy"


# ── Logging functions ─────────────────────────────────────────────────────────

log_info() {
    # stderr, not stdout: several helper functions below echo their return
    # value on stdout for $(...) capture, and log lines must not leak into it.
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [INFO]  $*" >&2
}

log_warn() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [WARN]  $*" >&2
}

log_error() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [ERROR] $*" >&2
    exit 1
}


# ── Preflight ──────────────────────────────────────────────────────────────────

_check_deps() {
    local missing=()
    for bin in aws jq curl openssl; do
        command -v "${bin}" >/dev/null 2>&1 || missing+=("${bin}")
    done
    if (( ${#missing[@]} > 0 )); then
        log_error "Missing required tools: ${missing[*]}"
    fi

    [[ -f "${VERSIONS_TF}" ]] || log_error "Terraform environment '${ENV}' not found at ${TF_ENV_DIR}."
    [[ -f "${TFC_CREDENTIALS_FILE}" ]] || log_error "No HCP Terraform token found at ${TFC_CREDENTIALS_FILE}. Run 'terraform login' first."

    aws sts get-caller-identity >/dev/null 2>&1 \
        || log_error "AWS credentials not active in this shell. Run 'aws sso login' / set AWS_PROFILE, then retry."
}

# Resolve the HCP Terraform org: TF_CLOUD_ORGANIZATION env var wins, else
# parsed out of the environment's versions.tf cloud block.
_resolve_org() {
    if [[ -n "${TF_CLOUD_ORGANIZATION:-}" ]]; then
        echo "${TF_CLOUD_ORGANIZATION}"
        return
    fi
    local org
    org=$(grep -m1 'organization *= *"' "${VERSIONS_TF}" | sed -E 's/.*organization *= *"([^"]*)".*/\1/')
    if [[ -z "${org}" || "${org}" == "REPLACE_WITH_YOUR_TF_CLOUD_ORG" ]]; then
        log_error "No HCP Terraform org configured for ENV=${ENV}. Set the 'organization' in ${VERSIONS_TF} (or pass TF_CLOUD_ORGANIZATION=... )."
    fi
    echo "${org}"
}

_resolve_workspace() {
    if [[ -n "${TF_WORKSPACE_NAME:-}" ]]; then
        echo "${TF_WORKSPACE_NAME}"
        return
    fi
    local ws
    ws=$(grep -m1 'name *= *"' "${VERSIONS_TF}" | sed -E 's/.*name *= *"([^"]*)".*/\1/')
    [[ -n "${ws}" ]] || log_error "Could not determine workspace name from ${VERSIONS_TF}. Pass TF_WORKSPACE_NAME=... explicitly."
    echo "${ws}"
}


# ── HCP Terraform API helpers ─────────────────────────────────────────────────

TFC_TOKEN=""
_tfc_token() {
    if [[ -z "${TFC_TOKEN}" ]]; then
        TFC_TOKEN=$(jq -r --arg h "${TFC_HOSTNAME}" '.credentials[$h].token' "${TFC_CREDENTIALS_FILE}")
        [[ -n "${TFC_TOKEN}" && "${TFC_TOKEN}" != "null" ]] \
            || log_error "No token for ${TFC_HOSTNAME} in ${TFC_CREDENTIALS_FILE}. Run 'terraform login'."
    fi
    echo "${TFC_TOKEN}"
}

_tfc_api() {
    local method=$1 path=$2 data=${3:-}
    local args=(-sS --fail-with-body -X "${method}"
        --header "Authorization: Bearer $(_tfc_token)"
        --header "Content-Type: application/vnd.api+json")
    [[ -n "${data}" ]] && args+=(--data "${data}")
    curl "${args[@]}" "https://${TFC_HOSTNAME}/api/v2${path}"
}

# Look up the workspace and print its id, then its project name, tab-separated.
_lookup_workspace() {
    local org=$1 ws=$2
    local resp ws_id project_id project_name
    resp=$(_tfc_api GET "/organizations/${org}/workspaces/${ws}") \
        || log_error "Workspace '${ws}' not found in org '${org}'. Run 'make tf-init ENV=${ENV}' first to create it."
    ws_id=$(jq -r '.data.id' <<<"${resp}")
    project_id=$(jq -r '.data.relationships.project.data.id' <<<"${resp}")
    project_name=$(_tfc_api GET "/projects/${project_id}" | jq -r '.data.attributes.name')
    printf '%s\t%s\n' "${ws_id}" "${project_name}"
}

# Create the workspace var if the key is missing, else patch its value in place.
_upsert_workspace_var() {
    local ws_id=$1 key=$2 value=$3 description=$4
    local existing_id
    existing_id=$(_tfc_api GET "/workspaces/${ws_id}/vars" \
        | jq -r --arg k "${key}" '.data[] | select(.attributes.key == $k) | .id')

    local payload
    payload=$(jq -n --arg k "${key}" --arg v "${value}" --arg d "${description}" --arg ws "${ws_id}" '
        {data: {type: "vars", attributes: {key: $k, value: $v, category: "env", sensitive: false, description: $d},
                relationships: {workspace: {data: {id: $ws, type: "workspaces"}}}}}')

    if [[ -n "${existing_id}" ]]; then
        log_info "Updating workspace var ${key}..."
        _tfc_api PATCH "/workspaces/${ws_id}/vars/${existing_id}" "${payload}" >/dev/null
    else
        log_info "Creating workspace var ${key}..."
        _tfc_api POST "/vars" "${payload}" >/dev/null
    fi
}


# ── AWS IAM helpers ────────────────────────────────────────────────────────────

# Fetch the SHA1 thumbprint of the top CA in app.terraform.io's chain — the
# value AWS's OIDC provider registration expects.
_fetch_thumbprint() {
    echo | openssl s_client -servername "${TFC_HOSTNAME}" -showcerts -connect "${TFC_HOSTNAME}:443" 2>/dev/null \
        | python3 -c '
import re, sys
data = sys.stdin.read()
certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", data, re.S)
print(certs[-1])' \
        | openssl x509 -noout -fingerprint -sha1 \
        | sed -E 's/.*Fingerprint=//; s/://g' | tr 'A-F' 'a-f'
}

# Ensure the OIDC provider for app.terraform.io exists; print its ARN.
_ensure_oidc_provider() {
    local account_id=$1
    local arn="arn:aws:iam::${account_id}:oidc-provider/${TFC_HOSTNAME}"

    if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${arn}" >/dev/null 2>&1; then
        log_info "OIDC provider for ${TFC_HOSTNAME} already exists."
    else
        log_info "Creating OIDC provider for ${TFC_HOSTNAME}..."
        local thumbprint
        thumbprint=$(_fetch_thumbprint)
        aws iam create-open-id-connect-provider \
            --url "https://${TFC_HOSTNAME}" \
            --client-id-list "${TFC_AUDIENCE}" \
            --thumbprint-list "${thumbprint}" \
            --tags Key=Name,Value="hcp-terraform-${TFC_HOSTNAME//./-}" >/dev/null
    fi
    echo "${arn}"
}

# Create the role if missing, else update its trust policy in place.
_ensure_role() {
    local account_id=$1 oidc_arn=$2 org=$3 project=$4 ws=$5
    local trust_policy
    trust_policy=$(jq -n --arg fed "${oidc_arn}" --arg aud "${TFC_AUDIENCE}" \
        --arg sub "organization:${org}:project:${project}:workspace:${ws}:run_phase:*" '
        {Version: "2012-10-17", Statement: [{
            Effect: "Allow",
            Principal: {Federated: $fed},
            Action: "sts:AssumeRoleWithWebIdentity",
            Condition: {
                StringEquals: {("app.terraform.io:aud"): $aud},
                StringLike: {("app.terraform.io:sub"): $sub}
            }
        }]}')

    if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
        log_info "Updating trust policy for role ${ROLE_NAME}..."
        aws iam update-assume-role-policy --role-name "${ROLE_NAME}" --policy-document "${trust_policy}"
    else
        log_info "Creating role ${ROLE_NAME}..."
        aws iam create-role \
            --role-name "${ROLE_NAME}" \
            --assume-role-policy-document "${trust_policy}" \
            --description "HCP Terraform OIDC role for workspace ${ws}" >/dev/null
    fi
    echo "arn:aws:iam::${account_id}:role/${ROLE_NAME}"
}

# Create the managed policy if missing, else publish a new default version
# (pruning the oldest non-default version first if already at AWS's 5-version cap).
_ensure_policy() {
    local account_id=$1
    local policy_arn="arn:aws:iam::${account_id}:policy/${POLICY_NAME}"
    local permissions_policy
    permissions_policy=$(jq -n '
        {Version: "2012-10-17", Statement: [
            {Sid: "Ec2Networking", Effect: "Allow", Resource: "*", Action: [
                "ec2:Describe*",
                "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
                "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
                "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
                "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
                "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
                "ec2:CreateTags", "ec2:DeleteTags"
            ]},
            {Sid: "Rds", Effect: "Allow", Resource: "*", Action: [
                "rds:Describe*", "rds:ListTagsForResource",
                "rds:CreateDBCluster", "rds:DeleteDBCluster", "rds:ModifyDBCluster",
                "rds:CreateDBInstance", "rds:DeleteDBInstance", "rds:ModifyDBInstance",
                "rds:CreateDBSubnetGroup", "rds:DeleteDBSubnetGroup", "rds:ModifyDBSubnetGroup",
                "rds:CreateDBClusterParameterGroup", "rds:DeleteDBClusterParameterGroup", "rds:ModifyDBClusterParameterGroup",
                "rds:CreateDBParameterGroup", "rds:DeleteDBParameterGroup", "rds:ModifyDBParameterGroup",
                "rds:AddTagsToResource", "rds:RemoveTagsFromResource"
            ]},
            {Sid: "SecretsManager", Effect: "Allow", Resource: "*", Action: [
                "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:UpdateSecret",
                "secretsmanager:PutSecretValue", "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret",
                "secretsmanager:TagResource", "secretsmanager:UntagResource", "secretsmanager:ListSecretVersionIds",
                "secretsmanager:GetResourcePolicy"
            ]},
            {Sid: "IamMonitoringRole", Effect: "Allow", Resource: "arn:aws:iam::*:role/*-rds-monitoring-role", Action: [
                "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
                "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
                "iam:AttachRolePolicy", "iam:DetachRolePolicy"
            ]},
            {Sid: "PassMonitoringRole", Effect: "Allow", Resource: "arn:aws:iam::*:role/*-rds-monitoring-role",
             Action: "iam:PassRole",
             Condition: {StringEquals: {("iam:PassedToService"): "monitoring.rds.amazonaws.com"}}}
        ]}')

    if aws iam get-policy --policy-arn "${policy_arn}" >/dev/null 2>&1; then
        log_info "Policy ${POLICY_NAME} exists — publishing updated version..."
        local versions non_default_count oldest_version
        versions=$(aws iam list-policy-versions --policy-arn "${policy_arn}" --output json)
        non_default_count=$(jq '[.Versions[] | select(.IsDefaultVersion == false)] | length' <<<"${versions}")
        if (( non_default_count >= 4 )); then
            oldest_version=$(jq -r '[.Versions[] | select(.IsDefaultVersion == false)] | sort_by(.CreateDate) | first | .VersionId' <<<"${versions}")
            log_info "Pruning oldest policy version ${oldest_version} (at AWS's 5-version cap)..."
            aws iam delete-policy-version --policy-arn "${policy_arn}" --version-id "${oldest_version}"
        fi
        aws iam create-policy-version --policy-arn "${policy_arn}" --policy-document "${permissions_policy}" --set-as-default >/dev/null
    else
        log_info "Creating policy ${POLICY_NAME}..."
        aws iam create-policy \
            --policy-name "${POLICY_NAME}" \
            --policy-document "${permissions_policy}" \
            --description "Permissions for HCP Terraform workspace (aurora module) — ${ENV}" >/dev/null
    fi
    echo "${policy_arn}"
}


# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    _check_deps

    local org ws account_id
    org=$(_resolve_org)
    ws=$(_resolve_workspace)
    account_id=$(aws sts get-caller-identity --query Account --output text)

    log_info "=== HCP Terraform OIDC bootstrap: ENV=${ENV} ==="
    log_info "AWS account   : ${account_id}"
    log_info "HCP org       : ${org}"
    log_info "HCP workspace : ${ws}"

    local ws_id project_name
    IFS=$'\t' read -r ws_id project_name < <(_lookup_workspace "${org}" "${ws}")
    log_info "HCP project   : ${project_name}"

    local oidc_arn role_arn policy_arn
    oidc_arn=$(_ensure_oidc_provider "${account_id}")
    role_arn=$(_ensure_role "${account_id}" "${oidc_arn}" "${org}" "${project_name}" "${ws}")
    policy_arn=$(_ensure_policy "${account_id}")

    log_info "Attaching policy to role..."
    aws iam attach-role-policy --role-name "${ROLE_NAME}" --policy-arn "${policy_arn}"

    _upsert_workspace_var "${ws_id}" "TFC_AWS_PROVIDER_AUTH" "true" \
        "Enables HCP Terraform dynamic credentials for AWS"
    _upsert_workspace_var "${ws_id}" "TFC_AWS_RUN_ROLE_ARN" "${role_arn}" \
        "IAM role HCP Terraform assumes via OIDC for AWS access"

    log_info "=== Done ==="
    log_info "Role   : ${role_arn}"
    log_info "Policy : ${policy_arn}"
    log_info "Run 'make tf-plan ENV=${ENV}' to verify."
}

main "$@"
