#!/usr/bin/env bash
# Lint, package src/, and deploy the stack.
# Usage: ./scripts/deploy.sh [stage] [region]
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
PROJECT="03-rate-limiter"
STACK="rate-limiter-${STAGE}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${DIR}/infrastructure/template.yaml"
PACKAGED="${DIR}/build/packaged.yaml"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="cfn-artifacts-${ACCOUNT}-${REGION}"
echo "Account: ${ACCOUNT}  Region: ${REGION}  Stack: ${STACK}"

# 1. Lint (skipped with a warning if cfn-lint is not installed).
if command -v cfn-lint >/dev/null; then
  cfn-lint "$TEMPLATE"
else
  echo "WARN: cfn-lint not installed (pip install cfn-lint); skipping lint."
fi

# 2. Private artifact bucket for Lambda zips (created once per account/region).
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

# 3. Validate, package, deploy.
aws cloudformation validate-template --region "$REGION" --template-body "file://${TEMPLATE}" >/dev/null
mkdir -p "${DIR}/build"
aws cloudformation package --region "$REGION" \
  --template-file "$TEMPLATE" \
  --s3-bucket "$BUCKET" --s3-prefix "$PROJECT" \
  --output-template-file "$PACKAGED" >/dev/null

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$PACKAGED" \
  --parameter-overrides "Stage=${STAGE}" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --tags "Project=${PROJECT}" "Stage=${STAGE}"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
