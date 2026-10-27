#!/usr/bin/env bash
# Delete the six Ad Creative Scorer stacks in reverse order.
# Usage: ./scripts/destroy.sh <env> <region>
#
# The two S3 buckets and the KMS key have DeletionPolicy: Retain, so they
# survive stack deletion. After the stacks are gone this script offers to
# empty (all object versions and delete markers) and delete both buckets.
# The KMS key is never deleted here; the script prints the command.
set -euo pipefail

ENV="${1:-}"
REGION="${2:-}"
[[ -n "$ENV" && -n "$REGION" ]] || { echo "Usage: $0 <env> <region>" >&2; exit 2; }

PROJECT_NAME="ad-scorer"
STACKS=(monitoring ml orchestration api compute foundation)

read -r -p "Delete all ${PROJECT_NAME}-${ENV}-* stacks in ${REGION}? Scores in DynamoDB will be lost. [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 1; }

stack_exists() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" >/dev/null 2>&1
}

output() {  # output <stack> <OutputKey>
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

# Record the retained resources before the foundation stack (and its outputs) is gone.
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
FOUNDATION="${PROJECT_NAME}-${ENV}-foundation"
CREATIVE_BUCKET="${PROJECT_NAME}-creative-assets-${ACCOUNT}-${ENV}"
ML_BUCKET="${PROJECT_NAME}-ml-artifacts-${ACCOUNT}-${ENV}"
KMS_KEY_ARN=""
if stack_exists "$FOUNDATION"; then
  KMS_KEY_ARN="$(output "$FOUNDATION" KMSKeyArn)"
fi

for suffix in "${STACKS[@]}"; do
  stack="${PROJECT_NAME}-${ENV}-${suffix}"
  if ! stack_exists "$stack"; then
    echo "${stack}: does not exist, skipping"
    continue
  fi
  echo "Deleting ${stack} ..."
  aws cloudformation delete-stack --region "$REGION" --stack-name "$stack"
  aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$stack"
  echo "Deleted ${stack}."
done

empty_bucket() {  # delete every object version and delete marker, 1000 at a time
  local bucket="$1" payload
  while :; do
    payload="$(aws s3api list-object-versions --region "$REGION" --bucket "$bucket" \
      --max-items 1000 --output json | python3 -c '
import json, sys
d = json.load(sys.stdin)
objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
        for o in (d.get("Versions") or []) + (d.get("DeleteMarkers") or [])]
print(json.dumps({"Objects": objs, "Quiet": True}) if objs else "")')"
    [[ -n "$payload" ]] || break
    aws s3api delete-objects --region "$REGION" --bucket "$bucket" --delete "$payload" >/dev/null
  done
}

echo
echo "Retained resources (DeletionPolicy: Retain):"
echo "  s3://${CREATIVE_BUCKET}"
echo "  s3://${ML_BUCKET}"
echo "  KMS key ${KMS_KEY_ARN:-<unknown: foundation stack was already gone>}"

read -r -p "Empty and delete both buckets now? This permanently deletes uploaded creatives, training data and models. [y/N] " ok2
if [[ "$ok2" == "y" || "$ok2" == "Y" ]]; then
  for b in "$CREATIVE_BUCKET" "$ML_BUCKET"; do
    if aws s3api head-bucket --bucket "$b" --expected-bucket-owner "$ACCOUNT" 2>/dev/null; then
      echo "Emptying s3://${b} ..."
      empty_bucket "$b"
      aws s3api delete-bucket --region "$REGION" --bucket "$b"
      echo "Deleted s3://${b}."
    else
      echo "s3://${b}: not found, skipping"
    fi
  done
else
  echo "Buckets kept. To remove them later, empty every object version, then delete:"
  echo "  aws s3api list-object-versions --bucket <bucket>   # then delete-objects for each version and delete marker"
  echo "  aws s3api delete-bucket --bucket <bucket> --region ${REGION}"
fi

echo
echo "The KMS key is kept so any retained data stays decryptable (about \$1/month)."
echo "When nothing encrypted with it remains, schedule deletion:"
echo "  aws kms schedule-key-deletion --region ${REGION} --key-id ${KMS_KEY_ARN:-<key-arn>} --pending-window-in-days 7"
