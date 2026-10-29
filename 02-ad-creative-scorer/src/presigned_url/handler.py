"""POST /v1/upload: return a presigned S3 PUT URL for a new creative.

Body: {"content_type": "image/png", "campaign_id": "spring-sale"}
The URL is signed with this function's role, which may only PutObject under
uploads/ in the creative bucket.
"""
import json
import os
import re
import uuid

import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/quicktime",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return respond(400, {"error": "Body must be JSON"})
    if not isinstance(body, dict):
        return respond(400, {"error": "Body must be a JSON object"})

    content_type = body.get("content_type", "image/jpeg")
    campaign_id = body.get("campaign_id")

    if content_type not in ALLOWED_CONTENT_TYPES:
        return respond(400, {"error": "Unsupported content type"})
    # campaign_id becomes part of the S3 key: allow safe characters only.
    if not isinstance(campaign_id, str) or not SAFE_ID.match(campaign_id):
        return respond(400, {"error": "campaign_id must match ^[A-Za-z0-9_-]{1,64}$"})

    creative_id = str(uuid.uuid4())
    s3_key = f"uploads/{campaign_id}/{creative_id}"
    expires_in = int(os.environ.get("URL_EXPIRY_SECONDS", "3600"))

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": os.environ["CREATIVE_BUCKET"],
            "Key": s3_key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )

    return respond(200, {
        "creative_id": creative_id,
        "upload_url": upload_url,
        "s3_key": s3_key,
        "expires_in": expires_in,
        "content_type": content_type,
    })
