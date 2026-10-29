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

# Some org-managed accounts deny iam:DeleteRole to the deploying session, so
# the IAM roles fail to delete and the stack stops in DELETE_FAILED (which
# also blocks the next deploy). In that case, keep the failed roles (they cost
# nothing) and finish deleting the stack. Up to 3 rounds, because a role only
# becomes "failed" once CloudFormation reaches it.
for round in 1 2 3; do
  if aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK" 2>/dev/null; then
    echo "Deleted ${STACK}."
    break
  fi
  FAILED="$(aws cloudformation list-stack-resources --region "$REGION" --stack-name "$STACK" \
    --query "StackResourceSummaries[?ResourceStatus=='DELETE_FAILED'].LogicalResourceId" --output text)"
  [[ -n "$FAILED" ]] || { echo "Stack deletion failed for another reason; check the stack events."; exit 1; }
  echo "Could not delete: ${FAILED}. Keeping them and retrying (round ${round})."
  # shellcheck disable=SC2086  # word-split the list of logical ids on purpose
  aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK" --retain-resources $FAILED
done

# Anything kept behind (usually IAM roles) is listed so you can remove it later.
echo "Leftover roles (delete in the IAM console with credentials that allow it):"
aws iam list-roles --query "Roles[?starts_with(RoleName, '${STACK}-')].RoleName" --output text || true

# Remove only this project's/stage's objects; the shared bucket stays.
if aws s3api head-bucket --bucket "$BUCKET" --expected-bucket-owner "$ACCOUNT" 2>/dev/null; then
  aws s3 rm --region "$REGION" "s3://${BUCKET}/${PREFIX}" --recursive --only-show-errors
  echo "Removed s3://${BUCKET}/${PREFIX}"
fi
