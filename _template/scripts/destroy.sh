#!/usr/bin/env bash
# Delete the stack. Usage: ./scripts/destroy.sh [stage] [region]
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
STACK="{{slug}}-${STAGE}"

read -r -p "Delete stack ${STACK} in ${REGION}? Data in the stack will be lost. [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 1; }

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "Deleted ${STACK}."
