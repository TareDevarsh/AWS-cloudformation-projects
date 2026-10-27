# Workflow · 03 Rate Limiter

[← Project README](../README.md)

## Main request flow

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Entry point
    participant L as Compute
    participant D as Storage
    C->>G: request (SigV4)
    G->>G: verify signature
    G->>L: invoke
    L->>D: read / write
    D-->>L: result
    L-->>C: response
```

## Deploy lifecycle

```mermaid
stateDiagram-v2
    [*] --> Lint: ./scripts/deploy.sh
    Lint --> Packaging: cfn-lint ok
    Lint --> [*]: errors
    Packaging --> Deploying: src/ uploaded
    Deploying --> Deployed: CREATE/UPDATE_COMPLETE
    Deploying --> RolledBack: failure
    RolledBack --> Lint: fix
    Deployed --> Tested: integration tests
    Tested --> Deleted: ./scripts/destroy.sh
    Deleted --> [*]
```

## Test checks

| # | Check | Expected |
|---|-------|----------|
| 1 | Unsigned request | 403 |
| 2 | | |
