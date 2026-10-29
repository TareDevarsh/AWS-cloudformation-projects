#!/usr/bin/env bash
# Start a SageMaker XGBoost training job (CloudFormation has no training-job
# resource), wait for it, and print the model key to pass to deploy.sh.
# Usage: ./scripts/train.sh <env> <region>
#
# Needs the foundation stack and CSV training data (label first, no header) at
#   s3://<ml-artifacts-bucket>/training-data/train/
#   s3://<ml-artifacts-bucket>/training-data/validation/
# Hyperparameters can be overridden with env vars: HP_MAX_DEPTH, HP_ETA,
# HP_NUM_ROUND, HP_SUBSAMPLE, HP_OBJECTIVE. Also TRAINING_INSTANCE_TYPE,
# XGBOOST_VERSION (must match SageMakerXGBoostVersion in 05-sagemaker.yaml).
set -euo pipefail

ENV="${1:-}"
REGION="${2:-}"
[[ -n "$ENV" && -n "$REGION" ]] || { echo "Usage: $0 <env> <region>" >&2; exit 2; }

PROJECT_NAME="ad-scorer"
FOUNDATION="${PROJECT_NAME}-${ENV}-foundation"
XGBOOST_VERSION="${XGBOOST_VERSION:-1.7-1}"
INSTANCE_TYPE="${TRAINING_INSTANCE_TYPE:-ml.m5.xlarge}"
HP_MAX_DEPTH="${HP_MAX_DEPTH:-6}"
HP_ETA="${HP_ETA:-0.1}"
HP_NUM_ROUND="${HP_NUM_ROUND:-100}"
HP_SUBSAMPLE="${HP_SUBSAMPLE:-0.8}"
HP_OBJECTIVE="${HP_OBJECTIVE:-binary:logistic}"

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$FOUNDATION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET="$(output MLArtifactsBucketName)"
KMS_KEY_ARN="$(output KMSKeyArn)"
ROLE_ARN="$(output SageMakerExecutionRoleArn)"
IMAGE_REPO="$(output XGBoostImageRepo)"
for v in BUCKET KMS_KEY_ARN ROLE_ARN IMAGE_REPO; do
  [[ -n "${!v}" && "${!v}" != "None" ]] || { echo "Missing ${v} output on ${FOUNDATION}" >&2; exit 1; }
done

for channel in train validation; do
  if [[ -z "$(aws s3 ls --region "$REGION" "s3://${BUCKET}/training-data/${channel}/" | head -n 1)" ]]; then
    echo "No data in s3://${BUCKET}/training-data/${channel}/ ; upload CSV files first." >&2
    exit 1
  fi
done

JOB="${PROJECT_NAME}-xgboost-${ENV}-$(date +%Y%m%d%H%M%S)"
echo "Starting training job ${JOB} (${INSTANCE_TYPE}, XGBoost ${XGBOOST_VERSION}, managed spot)"

channel() {  # channel <name>
  printf '{"ChannelName":"%s","DataSource":{"S3DataSource":{"S3DataType":"S3Prefix","S3Uri":"s3://%s/training-data/%s/","S3DataDistributionType":"FullyReplicated"}},"ContentType":"text/csv","CompressionType":"None"}' \
    "$1" "$BUCKET" "$1"
}

# Feature columns after the label must match feature_vector() in src/inference/handler.py:
# face_present, face_count, emotion_confidence, word_count, label_count,
# moderation_flag_count, color_saturation
aws sagemaker create-training-job --region "$REGION" \
  --training-job-name "$JOB" \
  --role-arn "$ROLE_ARN" \
  --algorithm-specification "TrainingImage=${IMAGE_REPO}:${XGBOOST_VERSION},TrainingInputMode=File" \
  --hyper-parameters "{\"max_depth\":\"${HP_MAX_DEPTH}\",\"eta\":\"${HP_ETA}\",\"num_round\":\"${HP_NUM_ROUND}\",\"subsample\":\"${HP_SUBSAMPLE}\",\"objective\":\"${HP_OBJECTIVE}\",\"eval_metric\":\"auc\"}" \
  --input-data-config "[$(channel train),$(channel validation)]" \
  --output-data-config "S3OutputPath=s3://${BUCKET}/models/xgboost-ctr/,KmsKeyId=${KMS_KEY_ARN}" \
  --resource-config "InstanceType=${INSTANCE_TYPE},InstanceCount=1,VolumeSizeInGB=20,VolumeKmsKeyId=${KMS_KEY_ARN}" \
  --stopping-condition "MaxRuntimeInSeconds=3600,MaxWaitTimeInSeconds=7200" \
  --enable-managed-spot-training \
  --checkpoint-config "S3Uri=s3://${BUCKET}/checkpoints/xgboost-ctr/" \
  --enable-inter-container-traffic-encryption \
  --enable-network-isolation \
  --tags "Key=Project,Value=${PROJECT_NAME}" "Key=Environment,Value=${ENV}" >/dev/null

echo "Waiting for ${JOB} (polls every 2 minutes) ..."
aws sagemaker wait training-job-completed-or-stopped --region "$REGION" --training-job-name "$JOB" || true

STATUS="$(aws sagemaker describe-training-job --region "$REGION" --training-job-name "$JOB" \
  --query TrainingJobStatus --output text)"
if [[ "$STATUS" != "Completed" ]]; then
  REASON="$(aws sagemaker describe-training-job --region "$REGION" --training-job-name "$JOB" \
    --query FailureReason --output text)"
  echo "Training job ${JOB} ended with status ${STATUS}: ${REASON}" >&2
  exit 1
fi

ARTIFACT="$(aws sagemaker describe-training-job --region "$REGION" --training-job-name "$JOB" \
  --query ModelArtifacts.S3ModelArtifacts --output text)"
MODEL_S3_KEY="${ARTIFACT#s3://${BUCKET}/}"

echo
echo "Training complete."
echo "ModelS3Key: ${MODEL_S3_KEY}"
echo "Next: ./scripts/deploy.sh ${ENV} ${REGION} <alert-email> ${MODEL_S3_KEY}"
