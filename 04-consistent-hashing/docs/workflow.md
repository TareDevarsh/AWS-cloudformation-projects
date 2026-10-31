# Workflow · 04 Consistent Hashing
[← Project README](../README.md)

Step 1 builds the hash ring with virtual nodes and a routing API. It routes keys to servers but stores no key data, so nothing is replicated or copied when the ring changes.

## Data model
One DynamoDB table, one partition per ring (`pk = RING#default`). Vnode sort keys are the zero-padded 40-hex-digit SHA-1 position, so a `Query` on `begins_with(sk, "VNODE#")` returns the ring already sorted clockwise.

```mermaid
erDiagram
    SERVER ||--o{ VNODE : "owns vnodeCount of"
    SERVER {
        string pk "RING#default"
        string sk "SERVER#<serverId>"
        string serverId
        number vnodeCount "1-500, default 100"
    }
    VNODE {
        string pk "RING#default"
        string sk "VNODE#<sha1(serverId#vnodeIndex), 40 hex>"
        string serverId
        number vnodeIndex "0 .. vnodeCount-1"
    }
```

## Route a key
```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant L as ring Lambda
    participant D as DynamoDB
    C->>G: GET /route/{key}?n=3 (SigV4)
    G->>G: verify signature (AWS_IAM)
    G->>L: invoke
    L->>L: validate key (1-256 bytes), n (1-10)
    L->>D: Query pk=RING#default, sk begins_with VNODE#
    D-->>L: vnodes sorted by position
    L->>L: pos = SHA-1(key)
    L->>L: binary search first vnode >= pos (wrap to first)
    opt n given
        L->>L: walk clockwise, keep first n distinct serverIds
    end
    L-->>C: 200 {position, vnode, serverId, preferenceList}
```

## Add a server
```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant L as ring Lambda
    participant D as DynamoDB
    C->>G: POST /servers {serverId, vnodes} (SigV4)
    G->>L: invoke
    L->>L: validate serverId [A-Za-z0-9_.-]{1,64}, vnodes 1-500
    L->>D: PutItem SERVER#id (attribute_not_exists)
    alt already exists
        D-->>L: ConditionalCheckFailed
        L-->>C: 409
    else new
        L->>D: Query current vnodes (old ring)
        L->>L: positions = SHA-1("serverId#i") for i in 0..vnodes-1
        L->>D: BatchWriteItem VNODE# items
        L->>L: diff old ring vs new ring -> ranges that changed owner
        L-->>C: 201 {vnodesAdded, affectedRanges, ranges}
    end
```

Removing a server (`DELETE /servers/{serverId}`) is the reverse: load the ring, delete that server's vnodes and its `SERVER#` item, and return each range with `fromServer` (the removed one) and `toServer` (the next vnode clockwise).

## Deploy lifecycle
```mermaid
stateDiagram-v2
    [*] --> Lint: ./scripts/deploy.sh
    Lint --> Packaging: cfn-lint ok
    Lint --> [*]: errors
    Packaging --> Deploying: src/ring uploaded
    Deploying --> Deployed: CREATE/UPDATE_COMPLETE
    Deploying --> RolledBack: failure
    RolledBack --> Lint: fix
    Deployed --> Tested: integration tests
    Tested --> Deleted: ./scripts/destroy.sh
    Deleted --> [*]
```

## Test checks
[`tests/integration/test_api.py`](../tests/integration/test_api.py)

| # | Check | Expected |
|---|-------|----------|
| 1 | Unsigned `GET /servers` | 403 |
| 2 | Add 4 servers x 100 vnodes | 201, `vnodesAdded` = 100 |
| 3 | Add a duplicate server | 409 |
| 4 | Invalid `serverId` (`bad#id`) | 400 |
| 5 | `GET /servers` shares of the 4 test servers | sum ≈ 100% |
| 6 | `GET /route/user-1?n=3` | 3 distinct servers, first = owner |
| 7 | Remove one server | 200, `vnodesRemoved` = 100 |
| 8 | Re-route 60 keys | only keys of the removed server moved |
