#!/usr/bin/env bash
# Lint, package src/, and deploy the stack.
# Usage: ./scripts/deploy.sh [stage] [region]
#   QUORUM_N=3 QUORUM_W=2 QUORUM_R=2 MAX_CLOCK_ENTRIES=10 ./scripts/deploy.sh dev   # override defaults
#   MERKLE_BUCKETS=16 REPAIR_SCHEDULE_RATE='rate(1 hour)' REPAIR_SCHEDULE_ENABLED=ENABLED ./scripts/deploy.sh dev
#   GOSSIP_TICK_RATE='rate(1 minute)' GOSSIP_FAILURE_THRESHOLD=90 GOSSIP_ENABLED=ENABLED ./scripts/deploy.sh dev
#   BUILD_ONLY=1 ./scripts/deploy.sh                           # stage build/ only, no AWS calls
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
PROJECT="04-consistent-hashing"
STACK="consistent-hashing-${STAGE}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_TEMPLATE="${DIR}/infrastructure/template.yaml"
PACKAGED="${DIR}/build/packaged.yaml"

# 0. Stage sources: build/stage mirrors infrastructure/ + src/, and each function folder gets a
#    copy of src/shared/*.py (ring_math.py for both; vector_clock.py used by kv), so
#    `Code: ../src/<fn>/` zips the handler plus its shared modules.
STAGED="${DIR}/build/stage"
rm -rf "$STAGED"
mkdir -p "${STAGED}/infrastructure"
cp "$SRC_TEMPLATE" "${STAGED}/infrastructure/template.yaml"
for fn in ring kv; do
  mkdir -p "${STAGED}/src/${fn}"
  cp "${DIR}/src/${fn}/"*.py "${DIR}/src/shared/"*.py "${STAGED}/src/${fn}/"
done
TEMPLATE="${STAGED}/infrastructure/template.yaml"
if [[ "${BUILD_ONLY:-0}" == "1" ]]; then
  echo "Staged functions in ${STAGED}/src"
  exit 0
fi

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
  --parameter-overrides "Stage=${STAGE}" "N=${QUORUM_N:-3}" "W=${QUORUM_W:-2}" "R=${QUORUM_R:-2}" \
    "MaxClockEntries=${MAX_CLOCK_ENTRIES:-10}" "MerkleBuckets=${MERKLE_BUCKETS:-16}" \
    "RepairScheduleRate=${REPAIR_SCHEDULE_RATE:-rate(1 hour)}" \
    "RepairScheduleEnabled=${REPAIR_SCHEDULE_ENABLED:-ENABLED}" \
    "GossipTickRate=${GOSSIP_TICK_RATE:-rate(1 minute)}" \
    "GossipFailureThreshold=${GOSSIP_FAILURE_THRESHOLD:-90}" \
    "GossipEnabled=${GOSSIP_ENABLED:-ENABLED}" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --tags "Project=${PROJECT}" "Stage=${STAGE}"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
