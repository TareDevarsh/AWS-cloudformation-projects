#!/usr/bin/env bash
# Usage: ./scripts/deploy.sh [stage] [region]
set -euo pipefail
STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
PROJECT="NN-project-name"
STACK="${PROJECT}-${STAGE}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${DIR}/infrastructure/template.yaml"

aws cloudformation validate-template --region "$REGION" --template-body "file://${TEMPLATE}" >/dev/null
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file "$TEMPLATE" --parameter-overrides "Stage=${STAGE}" \
  --capabilities CAPABILITY_IAM --tags "Project=${PROJECT}" "Stage=${STAGE}"
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
