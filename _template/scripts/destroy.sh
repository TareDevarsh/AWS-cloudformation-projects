#!/usr/bin/env bash
# Usage: ./scripts/destroy.sh [stage] [region]
set -euo pipefail
STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
STACK="NN-project-name-${STAGE}"
read -r -p "Delete stack ${STACK} in ${REGION}? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 1; }
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "Deleted ${STACK}."
