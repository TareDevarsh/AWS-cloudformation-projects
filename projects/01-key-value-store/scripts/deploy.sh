#!/usr/bin/env bash
# Deploy the KV store stack. Usage: ./scripts/deploy.sh [stage] [region]
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
STACK="kv-store-${STAGE}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${DIR}/infrastructure/kv-store.yaml"

echo "Account: $(aws sts get-caller-identity --query Account --output text)  Region: ${REGION}  Stack: ${STACK}"

aws cloudformation validate-template --region "$REGION" --template-body "file://${TEMPLATE}" >/dev/null
echo "Template valid."

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$TEMPLATE" \
  --parameter-overrides "Stage=${STAGE}" \
  --capabilities CAPABILITY_IAM \
  --tags Project=01-key-value-store "Stage=${STAGE}"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
