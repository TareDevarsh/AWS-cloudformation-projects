# Workflow · 04 Consistent Hashing
[← Project README](../README.md)

Step 1 built the hash ring with virtual nodes and a routing API. Step 2 adds a kv coordinator Lambda that stores each key on N replicas with quorum writes (W) and reads (R), plus up/down switches that simulate server failure. Data is not moved when the ring changes.

## Data model
Two DynamoDB tables. The ring table has one partition per ring (`pk = RING#default`). Vnode sort keys are the zero-padded 40-hex-digit SHA-1 position, so a `Query` on `begins_with(sk, "VNODE#")` returns the ring already sorted clockwise.

```mermaid
erDiagram
    SERVER ||--o{ VNODE : "owns vnodeCount of"
    SERVER {
        string pk "RING#default"
        string sk "SERVER#<serverId>"
        string serverId
        number vnodeCount "1-500, default 100"
        string status "up | down (simulated failure)"
    }
    VNODE {
        string pk "RING#default"
        string sk "VNODE#<sha1(serverId#vnodeIndex), 40 hex>"
        string serverId
        number vnodeIndex "0 .. vnodeCount-1"
    }
    SERVER ||--o{ REPLICA : "stores"
    REPLICA {
        string serverId "partition key (data table)"
        string itemKey "sort key: the user key"
        string value "up to 10 KB"
        number version "integer counter, highest wins"
        string updatedAt "ISO-8601 UTC"
    }
```

The data table holds one item per replica: the same key on 3 servers is 3 items, `(s2, user-42)`, `(s0, user-42)`, `(s3, user-42)`. Each server gets its own partition, like one node's local storage. Ring math (`src/shared/ring_math.py`) is copied into both functions at package time.

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

## Quorum write (PUT)

Defaults N=3, W=2. Here the first replica is marked down, so the write succeeds with 2 of 3 acks.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant K as kv Lambda
    participant RT as Ring table
    participant D as Data table
    C->>G: PUT /kv/{key}?w=2 {value} (SigV4)
    G->>K: invoke
    K->>K: validate key, value <= 10 KB, w in 1..N
    K->>RT: Query pk=RING#default (vnodes + server status)
    K->>K: preference list = first N distinct servers clockwise from SHA-1(key)
    Note over K: s2 is down: counted as failed, not called
    alt up replicas < w
        K-->>C: 503 write not attempted, failed list
    else enough replicas up
        par read-before-write
            K->>D: GetItem (s0, key)
        and
            K->>D: GetItem (s3, key)
        end
        K->>K: version = highest version seen + 1
        par write replicas
            K->>D: PutItem (s0, key, value, version, updatedAt)
        and
            K->>D: PutItem (s3, key, value, version, updatedAt)
        end
        alt acks >= w
            K-->>C: 200 {preferenceList, acked [s0, s3], failed [s2 down], version, n, w}
        else acks < w
            K-->>C: 503 {acked, failed}
        end
    end
```

## Quorum read (GET)

Defaults R=2. s2 is back up but missed the last write, so it holds an older version.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as API Gateway
    participant K as kv Lambda
    participant RT as Ring table
    participant D as Data table
    C->>G: GET /kv/{key}?r=2 (SigV4)
    G->>K: invoke
    K->>RT: Query ring + server status
    K->>K: preference list [s2, s0, s3], drop down servers
    par read every up replica
        K->>D: GetItem (s2, key)
        D-->>K: v1 (stale)
    and
        K->>D: GetItem (s0, key)
        D-->>K: v2
    and
        K->>D: GetItem (s3, key)
        D-->>K: v2
    end
    K->>K: count first r responses in preference-list order (s2, s0)
    alt fewer than r responses
        K-->>C: 503 {replicas}
    else none of the counted replicas has the key
        K-->>C: 404 {replicas}
    else
        K->>K: winner = highest version among counted (v2 from s0)
        K-->>C: 200 {value v2, version 2, staleReplicas [s2], replicas [...]}
    end
```

With `?r=1` only s2 is counted, so the client gets the stale v1. That is the trade-off of `W + R <= N`: here W=2, R=1, N=3.

## Simulated failure

`POST /servers/{id}/down` runs `UpdateItem SET status = down` (condition `attribute_exists`, else 404) through the ring Lambda; `/up` sets it back. The kv Lambda reads `status` on every request, so the change applies at once.

## Deploy lifecycle
```mermaid
stateDiagram-v2
    [*] --> Staging: ./scripts/deploy.sh
    Staging --> Lint: src/shared copied into each function
    Lint --> Packaging: cfn-lint ok
    Lint --> [*]: errors
    Packaging --> Deploying: build/stage/src/{ring,kv} (handler + ring_math.py) uploaded
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
| 9 | Unsigned `PUT /kv/{key}` | 403 |
| 10 | `PUT` then `GET` a fresh key | 200, 3 acks, value round-trips |
| 11 | First replica down, `PUT` with W=2 | 200, version 2, down replica not in `acked` |
| 12 | Two replicas down, `PUT` with W=2 | 503 |
| 13 | Both back up, `GET ?r=1` | stale v1 from the replica that missed the write |
| 14 | `GET ?r=3` | latest v2, `staleReplicas` = that replica |
| 15 | `GET ?r=9` | 400 |
