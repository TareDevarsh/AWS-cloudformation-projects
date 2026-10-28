#!/usr/bin/env bash
# Delete the stack and this stage's uploaded artifacts.
# Usage: ./scripts/destroy.sh [stage] [region]
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
PROJECT="03-rate-limiter"
STACK="rate-limiter-${STAGE}"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="cfn-artifacts-${ACCOUNT}-${REGION}"
PREFIX="${PROJECT}/${STAGE}/"

echo "This deletes stack ${STACK} in ${REGION} (EC2, VPC, Lambda, security group incl. your manual IP rule)"
echo "and the artifacts under s3://${BUCKET}/${PREFIX}"
read -r -p "Continue? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 1; }

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
echo "Waiting for stack deletion..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "Deleted ${STACK}."

# Remove only this project's/stage's objects; the shared bucket stays.
if aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT" 2>/dev/null; then
  aws s3 rm --region "$REGION" "s3://${BUCKET}/${PREFIX}" --recursive --only-show-errors
  echo "Removed s3://${BUCKET}/${PREFIX}"
fi
