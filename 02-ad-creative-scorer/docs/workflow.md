# Workflow · 02 Ad Creative Scorer

[← Project README](../README.md) · [Architecture](architecture.md)

## 1. Get an upload URL and upload the creative

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant P as presigned-url Lambda
    participant S as S3 creative-assets
    C->>G: POST /v1/upload {"content_type":"image/png","campaign_id":"summer-2026"} (SigV4)
    G->>G: Verify signature + execute-api:Invoke, validate UploadRequest model
    alt not signed / not allowed
        G-->>C: 403 Forbidden
    else body fails the model
        G-->>C: 400 Bad Request
    else allowed
        G->>P: Proxy event
        P->>P: Check content_type and campaign_id pattern, new creative_id (UUID)
        P->>P: Sign PUT URL for uploads/summer-2026/creative_id (3600 s, local, no AWS call)
        P-->>C: 200 {creative_id, upload_url, s3_key, expires_in, content_type}
    end
    C->>S: PUT upload_url (Content-Type: image/png)
    S->>S: SSE-KMS encrypt, store version
    S-->>C: 200
```

## 2. Submit for scoring

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant T as pipeline-trigger Lambda
    participant P as SSM Parameter Store
    participant D as DynamoDB scores
    participant F as Step Functions
    C->>G: POST /v1/score {s3_bucket, s3_key, creative_id, campaign_id} (SigV4)
    G->>G: Verify signature, validate ScoreRequest model
    G->>T: Proxy event
    T->>T: s3_bucket is the creative bucket, s3_key is uploads/campaign/creative, IDs match the key
    alt check fails
        T-->>C: 400 {error}
    else valid
        T->>P: GetParameter state-machine-arn (first call only, then cached)
        P-->>T: ARN
        T->>D: PutItem {creativeId, scoredAt, campaignId, s3Key, status: PENDING}
        T->>F: StartExecution {creative_id, campaign_id, s3_bucket, s3_key, scored_at}
        F-->>T: executionArn
        T-->>C: 202 {creative_id, status: PENDING, execution_arn, scored_at}
    end
```

## 3. Pipeline run (async)

```mermaid
sequenceDiagram
    autonumber
    participant F as Step Functions
    participant X as feature-extraction Lambda
    participant R as Rekognition
    participant M as S3 ml-artifacts
    participant I as inference Lambda
    participant E as SageMaker endpoint
    participant D as DynamoDB scores
    participant B as EventBridge
    F->>F: ValidateInput (Choice)
    F->>X: ExtractFeatures (lambda:invoke)
    X->>R: DetectLabels, DetectFaces, DetectText, DetectModerationLabels (S3Object)
    R-->>X: labels, faces, words, moderation labels
    X->>M: PutObject features/creative_id.json
    X->>D: UpdateItem status FEATURES_EXTRACTED, featureS3Key
    X-->>F: features
    alt moderation_flags not empty
        F->>D: FlaggedForModeration: UpdateItem status FLAGGED, moderationFlags
    else clean creative
        F->>I: RunInference {creative_id, campaign_id, scored_at, features}
        I->>E: InvokeEndpoint text/csv (7 feature values)
        E-->>I: prediction
        I->>D: UpdateItem ctrScore, engagementScore, status SCORED, scoredAtFinal
        I-->>F: {status, ctr_score, engagement_score}
    end
    F->>B: PublishScoringEvent: CreativeScoringComplete {creative_id, campaign_id, status, result}
    Note over F,D: Any error in ExtractFeatures, FlaggedForModeration or RunInference goes to UpdateStatusFailed (status FAILED, errorDetails) and then PipelineFailed
```

## 4. Poll for the result

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant Q as get-score Lambda
    participant D as DynamoDB scores
    loop every few seconds until SCORED, FLAGGED or FAILED
        C->>G: GET /v1/score/creative_id (SigV4)
        G->>Q: Proxy event
        Q->>Q: creativeId matches the ID pattern, else 400
        Q->>D: Query creativeId, newest scoredAt first, Limit 1
        D-->>Q: item or none
        alt no item
            Q-->>C: 404 {error: creative not found}
        else found
            Q-->>C: 200 {creative_id, campaign_id, status, scored_at, ctr_score, engagement_score, moderation_flags}
        end
    end
```

## Step Functions state machine

Mirrors the ASL in [`04-step-functions.yaml`](../infrastructure/04-step-functions.yaml) (`AdScorerStateMachine`, EXPRESS).

```mermaid
stateDiagram-v2
    state "ValidateInput · Choice" as ValidateInput
    state "InvalidInput · Pass" as InvalidInput
    state "ExtractFeatures · Lambda" as ExtractFeatures
    state "ModerationGate · Choice" as ModerationGate
    state "FlaggedForModeration · DynamoDB updateItem FLAGGED" as FlaggedForModeration
    state "RunInference · Lambda" as RunInference
    state "PublishScoringEvent · EventBridge putEvents" as PublishScoringEvent
    state "UpdateStatusFailed · DynamoDB updateItem FAILED" as UpdateStatusFailed
    state "PipelineFailed · Fail ScoringFailed" as PipelineFailed

    [*] --> ValidateInput
    ValidateInput --> ExtractFeatures: all 5 fields are strings and s3_key matches uploads/*
    ValidateInput --> InvalidInput: only creative_id and scored_at usable
    ValidateInput --> PipelineFailed: Default, item cannot be identified
    InvalidInput --> UpdateStatusFailed: error set to InvalidInput
    ExtractFeatures --> ModerationGate: features in extracted
    ExtractFeatures --> UpdateStatusFailed: Catch States.ALL after retries
    ModerationGate --> FlaggedForModeration: first moderation flag present
    ModerationGate --> RunInference: Default
    FlaggedForModeration --> PublishScoringEvent: result.status FLAGGED
    FlaggedForModeration --> UpdateStatusFailed: Catch States.ALL
    RunInference --> PublishScoringEvent: result.status SCORED
    RunInference --> UpdateStatusFailed: Catch States.ALL after retries
    PublishScoringEvent --> [*]: End
    UpdateStatusFailed --> PipelineFailed
    PipelineFailed --> [*]
```

| State | Type | Retry | On error |
|-------|------|-------|----------|
| `ValidateInput` | Choice | — | — |
| `InvalidInput` | Pass (sets `$.error`) | — | — |
| `ExtractFeatures` | Task `lambda:invoke`, 300 s timeout | Lambda service/throttle errors and Rekognition throughput: 3 attempts, 5 s × 2 backoff, full jitter | `UpdateStatusFailed` |
| `ModerationGate` | Choice on `$.extracted.features.moderation_flags[0]` | — | — |
| `FlaggedForModeration` | Task `dynamodb:updateItem` | — | `UpdateStatusFailed` |
| `RunInference` | Task `lambda:invoke`, 60 s timeout | `Lambda.ServiceException`, `SageMaker.ModelError`: 2 attempts, 3 s × 1.5 | `UpdateStatusFailed` |
| `PublishScoringEvent` | Task `events:putEvents` (source `ad-scorer.scorer`) | — | none (execution fails; item keeps its SCORED/FLAGGED status) |
| `UpdateStatusFailed` | Task `dynamodb:updateItem` | — | — |
| `PipelineFailed` | Fail (`ScoringFailed`) | — | Counted by the `ExecutionsFailed` alarm |

## Deploy lifecycle

```mermaid
stateDiagram-v2
    [*] --> Lint: ./scripts/deploy.sh env region email
    Lint --> Package: cfn-lint passes or is not installed
    Lint --> [*]: lint error
    Package --> Stacks01to04: aws cloudformation package to cfn-artifacts bucket
    Stacks01to04 --> Stack06: no model key, stack 05 skipped
    Stacks01to04 --> RolledBack: stack failure
    RolledBack --> Lint: fix template
    Stack06 --> ConfirmEmail: confirm SNS subscription
    ConfirmEmail --> UploadData: CSVs to training-data/train and validation
    UploadData --> Training: ./scripts/train.sh env region
    Training --> UploadData: status not Completed, fix data
    Training --> DeployML: prints ModelS3Key
    DeployML --> Deployed: ./scripts/deploy.sh env region email ModelS3Key creates stack 05
    Deployed --> GrantAccess: attach ClientPolicyArn to caller
    GrantAccess --> Tested: python3 tests/integration/test_api.py env region
    Tested --> Deployed: change and redeploy
    Tested --> Training: retrain, then redeploy with new key
    Tested --> Destroyed: ./scripts/destroy.sh env region, stacks 06 to 01
    Destroyed --> BucketsDeleted: confirm empty and delete retained buckets
    Destroyed --> Retained: decline, buckets and KMS key kept
    BucketsDeleted --> [*]
    Retained --> [*]
```

Each `deploy.sh` run deploys stacks in order 01, 02, 03, 04, then 05 (only with a model key), then 06, using `--no-fail-on-empty-changeset`, so rerunning it only changes what changed. A new `ModelS3Key` replaces the model and endpoint config; the endpoint keeps its name. The KMS key always survives `destroy.sh`, and the script prints the `schedule-key-deletion` command.

## Integration test checks (`tests/integration/test_api.py`)

| # | Check | Expected |
|---|-------|----------|
| 1 | Unsigned `POST /v1/upload` | 403 (IAM gate) |
| 2 | Unsigned `GET /v1/score/{id}` | 403 (IAM gate) |
| 3 | `POST /v1/upload` with `campaign_id` `../evil` | 400 |
| 4 | `POST /v1/upload` | 200 with `upload_url` |
| 5 | `PUT` a generated 128×128 PNG to the presigned URL | 200 |
| 6 | `POST /v1/score` with a foreign bucket | 400 |
| 7 | `POST /v1/score` | 202, `status` `PENDING` |
| 8 | Poll `GET /v1/score/{id}` for up to 90 s | `SCORED` or `FLAGGED` |
| 9 | `GET /v1/score/does-not-exist-0000` | 404 |

Check 8 needs all six stacks, including a trained model in stack 05.
