"""Feature extraction Lambda (invoked by the Step Functions ExtractFeatures state).

Event: {creative_id, campaign_id, s3_bucket, s3_key, scored_at}
1. Calls Rekognition DetectLabels, DetectFaces, DetectText, DetectModerationLabels.
2. Writes the feature JSON to s3://<ML_ARTIFACTS_BUCKET>/features/<creative_id>.json.
3. Sets the DynamoDB item status to FEATURES_EXTRACTED.
4. Returns the feature dict to Step Functions.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

rekognition = boto3.client("rekognition")
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def handler(event, context):
    logger.info("Feature extraction invoked for %s", event.get("creative_id"))

    creative_id = event["creative_id"]
    s3_bucket = event["s3_bucket"]
    s3_key = event["s3_key"]

    # Only analyse objects in this project's upload area (Step Functions
    # catches the error and marks the item FAILED).
    if s3_bucket != os.environ["CREATIVE_BUCKET"] or not s3_key.startswith("uploads/"):
        raise ValueError("s3_bucket/s3_key outside the creative upload area")

    image = {"S3Object": {"Bucket": s3_bucket, "Name": s3_key}}

    # 1. Labels
    labels_response = rekognition.detect_labels(
        Image=image,
        MaxLabels=int(os.environ.get("REKOGNITION_LABELS_MAX", "20")),
        MinConfidence=70.0,
    )
    labels = [
        {"name": label["Name"], "confidence": round(label["Confidence"], 2)}
        for label in labels_response["Labels"]
    ]

    # 2. Faces and emotions
    face_count = 0
    dominant_emotion = None
    emotion_confidence = 0.0
    if os.environ.get("REKOGNITION_FACE_ENABLED", "true") == "true":
        faces_response = rekognition.detect_faces(Image=image, Attributes=["ALL"])
        face_count = len(faces_response["FaceDetails"])
        if face_count > 0:
            top_emotion = max(
                faces_response["FaceDetails"][0]["Emotions"],
                key=lambda e: e["Confidence"],
            )
            dominant_emotion = top_emotion["Type"]
            emotion_confidence = round(top_emotion["Confidence"], 2)

    # 3. Text
    word_count = 0
    if os.environ.get("REKOGNITION_TEXT_ENABLED", "true") == "true":
        text_response = rekognition.detect_text(Image=image)
        word_count = sum(1 for t in text_response["TextDetections"] if t["Type"] == "WORD")

    # 4. Moderation labels
    moderation_flags = []
    if os.environ.get("REKOGNITION_MODERATION_ENABLED", "true") == "true":
        mod_response = rekognition.detect_moderation_labels(Image=image, MinConfidence=60.0)
        moderation_flags = [m["Name"] for m in mod_response["ModerationLabels"]]

    features = {
        "creative_id": creative_id,
        "face_present": face_count > 0,
        "face_count": face_count,
        "dominant_emotion": dominant_emotion,
        "emotion_confidence": emotion_confidence,
        "word_count": word_count,
        "labels": labels,
        "moderation_flags": moderation_flags,
        # color_saturation: TODO, compute with PIL from a Lambda layer
    }

    # 5. Persist features for training/debugging
    feature_key = f"features/{creative_id}.json"
    s3.put_object(
        Bucket=os.environ["ML_ARTIFACTS_BUCKET"],
        Key=feature_key,
        Body=json.dumps(features),
        ContentType="application/json",
    )

    # 6. Status update
    dynamodb.Table(os.environ["SCORES_TABLE"]).update_item(
        Key={"creativeId": creative_id, "scoredAt": event["scored_at"]},
        UpdateExpression="SET #status = :s, featureS3Key = :k",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":s": "FEATURES_EXTRACTED", ":k": feature_key},
    )

    logger.info("Feature extraction complete for %s", creative_id)
    return features
