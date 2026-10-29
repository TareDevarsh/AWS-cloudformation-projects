"""GET /v1/score/{creativeId}: return the latest score record for a creative.

Read-only: queries the scores table by creativeId, newest scoredAt first.
"""
import json
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

table = boto3.resource("dynamodb").Table(os.environ.get("SCORES_TABLE", "unset"))

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# DynamoDB attribute -> response field
FIELDS = {
    "creativeId": "creative_id",
    "campaignId": "campaign_id",
    "status": "status",
    "scoredAt": "scored_at",
    "ctrScore": "ctr_score",
    "engagementScore": "engagement_score",
    "moderationFlags": "moderation_flags",
}


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Not serialisable: {type(value).__name__}")


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def handler(event, context):
    creative_id = (event.get("pathParameters") or {}).get("creativeId")
    if not isinstance(creative_id, str) or not SAFE_ID.match(creative_id):
        return respond(400, {"error": "creativeId must match ^[A-Za-z0-9_-]{1,64}$"})

    resp = table.query(
        KeyConditionExpression=Key("creativeId").eq(creative_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return respond(404, {"error": "creative not found"})

    item = items[0]
    result = {out: item[attr] for attr, out in FIELDS.items() if attr in item}
    flags = result.get("moderation_flags")
    if isinstance(flags, str):  # stored by Step Functions as a JSON string
        try:
            result["moderation_flags"] = json.loads(flags)
        except ValueError:
            pass
    return respond(200, result)
