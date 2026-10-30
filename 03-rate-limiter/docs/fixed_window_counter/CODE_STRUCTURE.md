# Code structure · fixed window counter

[← Fixed window counter README](README.md) · [Architecture](../architecture.md) · [Token bucket code structure](../token_bucket/CODE_STRUCTURE.md)

A map of every file in `src/fixed_window_counter/`, drawn as a Merkle tree: each file has a fingerprint (short SHA-256 hash), and each folder's fingerprint is made from its children's. Change one character anywhere and that file's hash, its folder's hash, and the root hash all change, so the tree shows exactly where code changed.

## The tree

```mermaid
flowchart LR
    ROOT["fixed_window_counter/<br/>43e15536"]
    ROOT --> SHARED["Shared by all languages"]
    ROOT --> PY["python/<br/>e5b544b1"]
    ROOT --> GO["go/<br/>48e33e7a"]
    ROOT --> ND["node/<br/>e16c9204"]
    SHARED --> LUA["fixed_window_counter.lua · 59512b14<br/>the algorithm, runs in Redis"]
    SHARED --> RULES["rules.yaml · 92e23c59<br/>limits and windows"]
    SHARED --> DC["docker-compose.yml · 0b90e1ec<br/>starts all 5 containers"]
    PY --> PYAPP["app.py · b5a072bc<br/>limiter server"]
    PY --> PYDF["Dockerfile · f6796bb0"]
    PY --> PYREQ["requirements.txt · 5feb5f68"]
    GO --> GOAPP["main.go · 870f86a4<br/>limiter server"]
    GO --> GODF["Dockerfile · fc247689"]
    GO --> GOMOD["go.mod · 9b4a6a2a"]
    ND --> NDAPP["server.js · 23a064a0<br/>limiter server"]
    ND --> NDDF["Dockerfile · c17659de"]
    ND --> NDPKG["package.json · d0d1ce3e<br/>package-lock.json · 6791341d"]
```

"Shared by all languages" is a grouping for reading only; those files sit directly in `fixed_window_counter/`. The nginx config is **not** in this folder: the compose file reuses the one in [`src/token_bucket/router/`](../token_bucket/CODE_STRUCTURE.md).

## What each file does

| File | Job | Used by |
|------|-----|---------|
| `fixed_window_counter.lua` | The algorithm: find the window, read every counter, add 1 to all only if all have room | Every limiter sends it to Redis |
| `rules.yaml` | Which counters exist (`user`, `cookie`, `ip`, `login`), their limit and window | Every limiter, loaded into memory from S3 |
| `docker-compose.yml` | Starts router + 3 limiters + Redis; only the router publishes a port | Docker on the EC2 |
| `python/app.py` · `go/main.go` · `node/server.js` | Same job in 3 languages: read caller ids, pick matching rules, one Redis call, then call the backend or return 429 | Their own container |
| `*/Dockerfile` | How to build each container image (no logic) | `docker compose build` |
| `requirements.txt` · `go.mod` · `package*.json` | Pinned libraries for each language | The Docker build |

## How the hashes are made

- **File:** SHA-256 of the file's bytes.
- **Folder:** SHA-256 of its children's `name:hash` lines, sorted by name.
- **Shown:** first 8 characters. Skipped: `node_modules/`, `__pycache__/`, `.venv/`, and hidden files. This map and the README live in `docs/`, so they are not part of the hashes.

To recompute them after a change, from `src/`:

```bash
python3 -c "
import hashlib,os
S={'node_modules','__pycache__','.venv'}
def t(p,d=0):
    if os.path.isfile(p):
        x=hashlib.sha256(open(p,'rb').read()).hexdigest();print('  '*d+os.path.basename(p),x[:8]);return x
    k=sorted(i for i in os.listdir(p) if i not in S and not i.startswith('.'))
    x=hashlib.sha256('\n'.join(f'{i}:{t(os.path.join(p,i),d+1)}' for i in k).encode()).hexdigest()
    print('  '*d+os.path.basename(p)+'/',x[:8]);return x
t('fixed_window_counter')"
```
