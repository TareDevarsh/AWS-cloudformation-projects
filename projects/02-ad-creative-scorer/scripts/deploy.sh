#!/usr/bin/env bash
# Package src/ and deploy the six Ad Creative Scorer stacks in order.
# Usage: ./scripts/deploy.sh <env> <region> <alert-email> [model-s3-key]
#   env           dev | staging | prod
#   alert-email   required; receives CloudWatch alarm notifications
#   model-s3-key  key printed by scripts/train.sh; stack 05 (SageMaker) is
#                 skipped until you pass it
set -euo pipefail

usage() { echo "Usage: $0 <env> <region> <alert-email> [model-s3-key]" >&2; exit 2; }

ENV="${1:-}"
REGION="${2:-}"
ALERT_EMAIL="${3:-}"
MODEL_S3_KEY="${4:-}"
[[ -n "$ENV" && -n "$REGION" && -n "$ALERT_EMAIL" ]] || usage
[[ "$ENV" =~ ^(dev|staging|prod)$ ]] || { echo "env must be dev, staging or prod" >&2; exit 2; }
[[ "$ALERT_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] \
  || { echo "alert-email '${ALERT_EMAIL}' is not a valid address" >&2; exit 2; }

PROJECT_NAME="ad-scorer"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
INFRA="${DIR}/infrastructure"
BUILD="${DIR}/build"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="cfn-artifacts-${ACCOUNT}-${REGION}"
echo "Account: ${ACCOUNT}  Region: ${REGION}  Environment: ${ENV}"

# 1. Private artifact bucket for Lambda zips (created once per account/region).
if ! aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT" 2>/dev/null; then
  echo "Creating private artifact bucket ${BUCKET}"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"expire-old-artifacts","Status":"Enabled","Filter":{},"Expiration":{"Days":30}}]}'
fi

mkdir -p "$BUILD"

# deploy_stack <template-file> <stack-suffix> [Key=Value ...]
deploy_stack() {
  local template="$1" suffix="$2"; shift 2
  local stack="${PROJECT_NAME}-${ENV}-${suffix}"
  local packaged="${BUILD}/${template%.yaml}.packaged.yaml"
  echo
  echo "=== ${stack} (${template}) ==="
  if command -v cfn-lint >/dev/null 2>&1; then
    cfn-lint "${INFRA}/${template}"
  else
    echo "cfn-lint not on PATH; skipping lint"
  fi
  aws cloudformation package --region "$REGION" \
    --template-file "${INFRA}/${template}" \
    --s3-bucket "$BUCKET" --s3-prefix "$PROJECT_NAME" \
    --output-template-file "$packaged" >/dev/null
  aws cloudformation deploy --region "$REGION" \
    --stack-name "$stack" \
    --template-file "$packaged" \
    --s3-bucket "$BUCKET" --s3-prefix "$PROJECT_NAME" \
    --parameter-overrides "ProjectName=${PROJECT_NAME}" "Environment=${ENV}" "$@" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --tags "Project=${PROJECT_NAME}" "Environment=${ENV}"
}

output() {  # output <stack-suffix> <OutputKey>
  aws cloudformation describe-stacks --region "$REGION" \
    --stack-name "${PROJECT_NAME}-${ENV}-$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

# 2. Ordered deploy. Each stack imports exports from the ones before it.
deploy_stack 01-foundation.yaml          foundation
deploy_stack 02-compute-and-storage.yaml compute
deploy_stack 03-api-gateway.yaml         api
deploy_stack 04-step-functions.yaml      orchestration

if [[ -n "$MODEL_S3_KEY" ]]; then
  deploy_stack 05-sagemaker.yaml ml "ModelS3Key=${MODEL_S3_KEY}"
else
  echo
  echo "Skipping 05-sagemaker: no model yet. Upload training data, run"
  echo "  ./scripts/train.sh ${ENV} ${REGION}"
  echo "then rerun this script with the model key it prints."
fi

deploy_stack 06-monitoring.yaml monitoring "AlertEmail=${ALERT_EMAIL}"

echo
echo "API endpoint:      $(output api APIEndpoint)"
echo "Client policy ARN: $(output api ClientPolicyArn)  (attach to callers; requests must be SigV4-signed)"
echo "Dashboard:         $(output monitoring DashboardURL)"
echo "Confirm the SNS subscription email sent to ${ALERT_EMAIL}."
