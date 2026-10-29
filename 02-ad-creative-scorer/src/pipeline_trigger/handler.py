"""POST /v1/score: seed a PENDING item and start the scoring state machine.

Body: {"s3_bucket": "<creative bucket>", "s3_key": "uploads/<campaign>/<creative>",
       "creative_id": "<creative>", "campaign_id": "<campaign>" (optional)}
Only objects under uploads/ in this project's creative bucket are accepted.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

sfn = boto3.client("stepfunctions")
ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
UPLOAD_KEY = re.compile(r"^uploads/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9-]{1,64})$")

_state_machine_arn = None


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def state_machine_arn():
    global _state_machine_arn
    if _state_machine_arn is None:
        resp = ssm.get_parameter(Name=os.environ["STATE_MACHINE_ARN_PARAM"])
        _state_machine_arn = resp["Parameter"]["Value"]
    return _state_machine_arn


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return respond(400, {"error": "Body must be JSON"})
    if not isinstance(body, dict):
        return respond(400, {"error": "Body must be a JSON object"})

    s3_bucket = body.get("s3_bucket")
    s3_key = body.get("s3_key")
    creative_id = body.get("creative_id")
    campaign_id = body.get("campaign_id")

    if s3_bucket != os.environ["CREATIVE_BUCKET"]:
        return respond(400, {"error": "s3_bucket must be the project's creative bucket"})
    match = UPLOAD_KEY.match(s3_key) if isinstance(s3_key, str) else None
    if not match:
        return respond(400, {"error": "s3_key must look like uploads/<campaign_id>/<creative_id>"})
    key_campaign, key_creative = match.groups()
    if not isinstance(creative_id, str) or creative_id != key_creative:
        return respond(400, {"error": "creative_id must match the last segment of s3_key"})
    if campaign_id is None:
        campaign_id = key_campaign
    if not isinstance(campaign_id, str) or not SAFE_ID.match(campaign_id) or campaign_id != key_campaign:
        return respond(400, {"error": "campaign_id must match the middle segment of s3_key"})

    scored_at = datetime.now(timezone.utc).isoformat()

    dynamodb.Table(os.environ["SCORES_TABLE"]).put_item(Item={
        "creativeId": creative_id,
        "scoredAt": scored_at,
        "campaignId": campaign_id,
        "s3Key": s3_key,
        "status": "PENDING",
    })

    execution = sfn.start_execution(
        stateMachineArn=state_machine_arn(),
        name=f"{creative_id}-{uuid.uuid4().hex[:8]}",  # <= 73 chars, limit is 80
        input=json.dumps({
            "creative_id": creative_id,
            "campaign_id": campaign_id,
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
            "scored_at": scored_at,
        }),
    )

    return respond(202, {
        "creative_id": creative_id,
        "status": "PENDING",
        "execution_arn": execution["executionArn"],
        "scored_at": scored_at,
    })
