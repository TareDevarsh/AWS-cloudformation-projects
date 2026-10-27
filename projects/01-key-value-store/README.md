# 01 · Key-Value Store

![Status](https://img.shields.io/badge/status-prototype-yellow)
![IaC](https://img.shields.io/badge/IaC-CloudFormation-FF4F8B?logo=amazonaws&logoColor=white)
![Auth](https://img.shields.io/badge/auth-IAM%20SigV4-232F3E?logo=amazonaws&logoColor=white)
![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)

[← Portfolio](../../README.md) · [Architecture](docs/architecture.md) · [Workflows](docs/workflow.md) · [Template](infrastructure/kv-store.yaml) · [Code](src/handler.py)

![Architecture](docs/images/architecture.png)

## Summary

A serverless key-value store with a REST API. Clients `PUT`, `GET`, `DELETE`, and list keys over HTTPS. Every request is authenticated with AWS IAM (SigV4). Values are any JSON, with an optional per-key TTL. The whole stack is one CloudFormation template.

| Fact | Value |
|------|-------|
| Status | 🟡 Prototype |
| AWS services | API Gateway (REST), Lambda (Python 3.12), DynamoDB, IAM, CloudWatch Logs |
| Auth | IAM / SigV4 (`AWS_IAM` on every method) |
| Infra as code | [`infrastructure/kv-store.yaml`](infrastructure/kv-store.yaml) |
| Cost at idle | ~$0 (all pay-per-request) |

## Architecture

The image above uses official AWS icons and is generated from [`docs/diagram.py`](docs/diagram.py). The same flow as a Mermaid diagram:

```mermaid
flowchart LR
    Client[Client<br/>SigV4-signed HTTPS] -->|execute-api:Invoke| APIGW[API Gateway<br/>REST · AWS_IAM auth<br/>throttle 10 rps]
    APIGW -->|AWS_PROXY| Lambda[Lambda<br/>kv-store-stage]
    Lambda -->|Get/Put/Delete/Scan| DDB[(DynamoDB<br/>pk: key · TTL: expiresAt)]
    Lambda --> Logs[CloudWatch Logs<br/>14-day retention]
    IAM[IAM<br/>KvClientPolicy] -.grants.-> Client
```

## How it works

1. Client signs the request with its IAM credentials.
2. API Gateway checks the signature and that the caller has `execute-api:Invoke` (granted by the `KvClientPolicy` output). Unsigned or unauthorized calls get `403`.
3. Lambda validates input and calls DynamoDB.
4. Response is JSON.

| Method | Path | Body | Result |
|--------|------|------|--------|
| `PUT` | `/kv/{key}` | `{"value": <json>, "ttlSeconds": 3600}` | `200` stored |
| `GET` | `/kv/{key}` | — | `200 {key, value, updatedAt}` or `404` |
| `DELETE` | `/kv/{key}` | — | `200` or `404` |
| `GET` | `/kv` | — | `200 {keys: [...]}` (first 100) |

Detailed flows: [docs/workflow.md](docs/workflow.md). Design decisions: [docs/architecture.md](docs/architecture.md).

## Repo layout

```text
01-key-value-store/
├── README.md                 ← this file
├── docs/
│   ├── architecture.md       ← components, data model, decisions
│   ├── workflow.md           ← sequence diagrams, deploy lifecycle
│   ├── diagram.py            ← diagram-as-code (AWS icons)
│   └── images/architecture.png
├── infrastructure/
│   └── kv-store.yaml         ← CloudFormation stack
├── src/
│   └── handler.py            ← Lambda handler
├── tests/
│   └── integration/
│       └── test_api.py       ← SigV4-signed end-to-end test
└── scripts/
    ├── deploy.sh             ← package src/ + deploy stack, print outputs
    └── destroy.sh            ← delete stack
```

## Deploy / Test / Teardown

```bash
cd projects/01-key-value-store

# 1. Deploy (stage defaults to dev, region to $AWS_REGION or us-east-1).
#    Creates a private artifact bucket cfn-artifacts-<account>-<region> on first run,
#    uploads src/ with `aws cloudformation package`, then deploys.
./scripts/deploy.sh dev us-east-1

# 2. Grant your IAM user/role access (ARN from the ClientPolicyArn output)
aws iam attach-user-policy --user-name <you> --policy-arn <ClientPolicyArn>
#   or: aws iam attach-role-policy --role-name <role> --policy-arn <ClientPolicyArn>

# 3. Test
pip install boto3
python3 tests/integration/test_api.py dev us-east-1

# 4. Tear down (deletes all stored data)
./scripts/destroy.sh dev us-east-1
```

## Cost & security notes

- **Cost:** DynamoDB on-demand, Lambda and API Gateway per-request. Idle cost is near zero; PITR adds a small per-GB charge.
- **Auth:** every method uses `AWS_IAM`. Nothing is anonymously reachable. The smoke test checks that an unsigned request gets `403`.
- **Least privilege:** the Lambda role can only touch this table and its own log group. The client policy only allows invoking this API's stage.
- **Data:** encrypted at rest (DynamoDB SSE), PITR on. `DeletionPolicy: Delete` because it's a prototype; set `Retain` before holding real data.
- **Artifacts:** Lambda zips go to a private bucket (Block Public Access on, SSE, 30-day expiry).
- **Abuse limits:** stage throttling 10 rps / burst 20; key ≤ 256 chars; value ≤ 350 KB.

## What I learned / next steps

- [ ] Add a conditional write (`If-Match` / version number) for optimistic concurrency
- [ ] Paginate `GET /kv` with `LastEvaluatedKey`
- [ ] Add CloudWatch alarms on Lambda errors and API 5xx
- [ ] Unit tests for `src/handler.py` with a DynamoDB stub
