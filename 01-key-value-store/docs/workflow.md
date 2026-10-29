# Workflow · 01 Key-Value Store

[← Project README](../README.md)

## PUT a key

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant L as Lambda
    participant D as DynamoDB
    C->>G: PUT /kv/greeting {"value":{...},"ttlSeconds":60} (SigV4)
    G->>G: Verify signature + execute-api:Invoke
    alt not signed / not allowed
        G-->>C: 403 Forbidden
    else allowed
        G->>L: Proxy event
        L->>L: Validate key, JSON body, size, ttl
        L->>D: PutItem {pk, value, updatedAt, expiresAt}
        D-->>L: OK
        L-->>G: 200 {key, updatedAt}
        G-->>C: 200
    end
```

## GET a key

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant L as Lambda
    participant D as DynamoDB
    C->>G: GET /kv/greeting (SigV4)
    G->>L: Proxy event
    L->>D: GetItem pk=greeting
    D-->>L: Item or empty
    alt missing or expired
        L-->>C: 404 not found
    else found
        L-->>C: 200 {key, value, updatedAt}
    end
```

`DELETE /kv/{key}` follows the same path with `DeleteItem` (`ReturnValues=ALL_OLD` → 404 if nothing existed). `GET /kv` runs a `Scan` limited to 100 keys.

## Deploy lifecycle

```mermaid
stateDiagram-v2
    [*] --> Validate: ./scripts/deploy.sh
    Validate --> Deploying: template valid
    Validate --> [*]: template error
    Deploying --> Deployed: CREATE/UPDATE_COMPLETE
    Deploying --> RolledBack: failure
    RolledBack --> Validate: fix template
    Deployed --> GrantAccess: attach KvClientPolicy
    GrantAccess --> Tested: python3 tests/integration/test_api.py
    Tested --> Deployed: change + redeploy
    Tested --> Deleted: ./scripts/destroy.sh
    Deleted --> [*]
```

## Smoke test checks (`tests/integration/test_api.py`)

| # | Check | Expected |
|---|-------|----------|
| 1 | Unsigned `GET /kv` | 403 (IAM gate works) |
| 2 | `PUT /kv/greeting` | 200 |
| 3 | `GET /kv/greeting` | 200, value matches |
| 4 | `GET /kv` | 200, contains `greeting` |
| 5 | `DELETE /kv/greeting` | 200 |
| 6 | `GET /kv/greeting` | 404 |
