#!/usr/bin/env bash
# Lint, upload code + rules, and deploy the rate limiter stack.
# Usage: ./scripts/deploy.sh [stage] [region] [algorithm]
#   algorithm: token_bucket (default), leaky_bucket (also creates the SQS queues),
#              sliding_window_log or fixed_window_counter
#   e.g.  ./scripts/deploy.sh dev us-east-1 leaky_bucket
#
# Re-running is safe:
#   - rules.yaml changed only -> re-uploaded; limiters pick it up within ~30 s
#     (S3 ETag check), the instance is NOT replaced.
#   - code changed -> new zip key -> CloudFormation replaces the instance
#     (new public IP; your security group rule stays).
set -euo pipefail

STAGE="${1:-dev}"
REGION="${2:-${AWS_REGION:-us-east-1}}"
# Which algorithm the instance runs; also the name of its folder under src/.
ALGORITHM="${3:-token_bucket}"
# Stop early on a typo, before anything is uploaded.
case "$ALGORITHM" in
  # The four algorithms that exist: nothing to do.
  token_bucket|leaky_bucket|sliding_window_log|fixed_window_counter) ;;
  # Anything else: explain and quit.
  *) echo "Unknown algorithm '${ALGORITHM}' (use token_bucket, leaky_bucket, sliding_window_log or fixed_window_counter)"; exit 1 ;;
esac
PROJECT="03-rate-limiter"
STACK="rate-limiter-${STAGE}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${DIR}/infrastructure/template.yaml"
BUILD="${DIR}/build"
PACKAGED="${BUILD}/packaged.yaml"
SRC="${DIR}/src"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="cfn-artifacts-${ACCOUNT}-${REGION}"
PREFIX="${PROJECT}/${STAGE}"
echo "Account: ${ACCOUNT}  Region: ${REGION}  Stack: ${STACK}  Algorithm: ${ALGORITHM}"

# 1. Lint (skipped with a warning if cfn-lint is not installed).
if command -v cfn-lint >/dev/null; then
  cfn-lint "$TEMPLATE"
else
  echo "WARN: cfn-lint not installed (pip install cfn-lint); skipping lint."
fi

# 2. Private artifact bucket (created once per account/region).
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

# 3. Zip the limiter code. All four algorithm folders go in the same zip (the
#    others reuse token_bucket/router/nginx.conf, and the leaky bucket also
#    token_bucket/token_bucket.lua, so the folders must stay side by side); the instance then runs the one
#    chosen by the Algorithm parameter. The S3 key includes a hash of the file
#    contents, so the instance is replaced only when the code actually changes.
mkdir -p "$BUILD"
rm -f "${BUILD}/app.zip"
(
  cd "$SRC"
  # Everything under the four algorithm folders, minus local junk and secrets.
  find token_bucket leaky_bucket sliding_window_log fixed_window_counter -type f \
    ! -path '*/node_modules/*' ! -path '*/__pycache__/*' ! -path '*/.venv/*' \
    ! -name '.env' ! -name '.DS_Store' | LC_ALL=C sort > "${BUILD}/files.txt"
  HASH="$(xargs shasum -a 256 < "${BUILD}/files.txt" | shasum -a 256 | cut -c1-12)"
  echo "$HASH" > "${BUILD}/app.hash"
  zip -q -X "${BUILD}/app.zip" -@ < "${BUILD}/files.txt"
)
APP_KEY="${PREFIX}/app-$(cat "${BUILD}/app.hash").zip"
RULES_KEY="${PREFIX}/rules.yaml"

echo "Uploading s3://${BUCKET}/${APP_KEY}"
aws s3 cp --region "$REGION" "${BUILD}/app.zip" "s3://${BUCKET}/${APP_KEY}" --only-show-errors
echo "Uploading s3://${BUCKET}/${RULES_KEY}"
aws s3 cp --region "$REGION" "${SRC}/${ALGORITHM}/rules.yaml" "s3://${BUCKET}/${RULES_KEY}" --only-show-errors

# 4. Validate, package the backend Lambda, deploy.
# The template uses the AWS::LanguageExtensions transform (Fn::ForEach). cfn-lint
# above expands it and checks it; here a failure is only a warning, because the
# server-side validation of templates with transforms is not guaranteed. The
# change set created by "deploy" below does the real check.
aws cloudformation validate-template --region "$REGION" --template-body "file://${TEMPLATE}" >/dev/null \
  || echo "WARN: validate-template failed (may be the transform); continuing, deploy checks again."
aws cloudformation package --region "$REGION" \
  --template-file "$TEMPLATE" \
  --s3-bucket "$BUCKET" --s3-prefix "$PREFIX" \
  --output-template-file "$PACKAGED" >/dev/null

echo "Deploying ${ALGORITHM} (first boot builds the images; allow ~5-15 min)..."
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$PACKAGED" \
  --parameter-overrides "Stage=${STAGE}" "ArtifactBucket=${BUCKET}" "AppKey=${APP_KEY}" "RulesKey=${RULES_KEY}" "Algorithm=${ALGORITHM}" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset \
  --tags "Project=${PROJECT}" "Stage=${STAGE}"

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table

# 5. Reminder: the security group has no inbound rules until you add one.
MYIP="$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null || echo '<your-ip>')"
cat <<EOF

Next: open the SecurityGroupConsole link above and add ONE inbound rule:
  Type: Custom TCP   Port: 8080   Source: ${MYIP}/32
(Your current public IP is shown here only; it is not saved anywhere.)
Then test:  python3 tests/integration/latency.py <Endpoint>        (token_bucket)
            python3 tests/integration/leaky_latency.py <Endpoint>  (leaky_bucket)
            python3 tests/integration/window_latency.py <Endpoint> ${ALGORITHM}  (window algorithms)
EOF
