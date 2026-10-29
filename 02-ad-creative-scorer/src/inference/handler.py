"""Inference Lambda (invoked by the Step Functions RunInference state).

Event: {creative_id, campaign_id, scored_at, features}
Calls the SageMaker XGBoost endpoint, writes the CTR and engagement scores
to DynamoDB, and returns {creative_id, ctr_score, engagement_score, status}.
"""
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

sagemaker_runtime = boto3.client("sagemaker-runtime")
dynamodb = boto3.resource("dynamodb")


def feature_vector(features):
    """Column order must match the training CSV (after the label column)."""
    return [
        int(features.get("face_present", False)),
        features.get("face_count", 0),
        features.get("emotion_confidence", 0.0),
        features.get("word_count", 0),
        len(features.get("labels", [])),
        len(features.get("moderation_flags", [])),
        features.get("color_saturation", 0.5),  # placeholder until computed
    ]


def parse_prediction(body):
    """Built-in XGBoost returns {"predictions": [{"score": x}]} for JSON, or a bare number for CSV."""
    text = body.decode("utf-8").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return float(text.split(",")[0])
    if isinstance(parsed, (int, float)):
        return float(parsed)
    first = parsed.get("predictions", [0.5])[0]
    if isinstance(first, dict):
        return float(first.get("score", 0.5))
    return float(first)


def handler(event, context):
    creative_id = event["creative_id"]
    logger.info("Inference invoked for %s", creative_id)

    csv_payload = ",".join(str(v) for v in feature_vector(event["features"]))
    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=os.environ["SAGEMAKER_ENDPOINT_NAME"],
        ContentType="text/csv",
        Accept="application/json",
        Body=csv_payload,
    )
    ctr_score = round(min(max(parse_prediction(response["Body"].read()), 0.0), 1.0), 4)
    engagement_score = round(ctr_score * 1.15, 4)  # simplified derived metric

    dynamodb.Table(os.environ["SCORES_TABLE"]).update_item(
        Key={"creativeId": creative_id, "scoredAt": event["scored_at"]},
        UpdateExpression=(
            "SET ctrScore = :ctr, engagementScore = :eng, "
            "#status = :s, scoredAtFinal = :ts"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":ctr": Decimal(str(ctr_score)),
            ":eng": Decimal(str(engagement_score)),
            ":s": "SCORED",
            ":ts": datetime.now(timezone.utc).isoformat(),
        },
    )

    result = {
        "creative_id": creative_id,
        "ctr_score": ctr_score,
        "engagement_score": engagement_score,
        "status": "SCORED",
    }
    logger.info("Inference complete: %s", json.dumps(result))
    return result
