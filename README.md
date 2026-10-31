# AWS CloudFormation Projects

![Projects](https://img.shields.io/badge/projects-4-blue)
![IaC](https://img.shields.io/badge/IaC-CloudFormation-FF4F8B?logo=amazonaws&logoColor=white)
![Auth](https://img.shields.io/badge/default%20auth-IAM%20SigV4-232F3E?logo=amazonaws&logoColor=white)
![Lint](https://img.shields.io/badge/cfn--lint-passing-brightgreen)

Hands-on AWS prototypes, each deployed with **CloudFormation** and documented so that a reviewer can tell what it does and how it works in a couple of minutes.
Every project is a numbered folder at the top of this repo (`01-…`, `02-…`) and is self-contained: it has its own README, an architecture image, Mermaid flow diagrams, templates, code, tests, and scripts.

## Projects

| # | Project | What it shows | AWS services | Status |
|---|---------|---------------|--------------|--------|
| 01 | [**Key-Value Store**](01-key-value-store/) | Serverless CRUD API over a NoSQL table, IAM-authenticated | API Gateway · Lambda · DynamoDB · IAM · CloudWatch | 🟡 Prototype |
| 02 | [**Ad Creative Scorer**](02-ad-creative-scorer/) | ML pipeline: upload an ad image → computer-vision features → CTR score | API Gateway · Lambda · Step Functions · Rekognition · SageMaker · DynamoDB · S3 · KMS · CloudWatch | 🟡 Prototype |
| 03 | [**Rate Limiter**](03-rate-limiter/) | Rate limiter middleware in front of a Lambda backend: token bucket, leaky bucket (SQS queue), sliding window log and fixed window counter (Redis), compared in Python, Go and Node | EC2 · Docker · Redis · S3 · SQS · Lambda | 🟡 Prototype |
| 04 | [**Consistent Hashing**](04-consistent-hashing/) | Hash ring with virtual nodes plus a quorum KV store: N replicas, W/R quorum reads and writes, vector clocks with conflict detection and server-side last-write-wins, sloppy quorum with hinted handoff for temporary failures, Merkle-tree anti-entropy repair for permanent failures, simulated gossip failure detection (heartbeats + scheduled tick), affected ranges on add/remove. Design docs cover a Dynamo-style KV store | API Gateway · Lambda · DynamoDB · EventBridge · IAM · CloudWatch | 🟡 Prototype |

Status legend: ⚪ Planned · 🟡 Prototype · 🟢 Deployed & tested

<table>
<tr>
<td width="50%" align="center"><a href="01-key-value-store/"><img src="01-key-value-store/docs/images/architecture.png" alt="01 Key-Value Store architecture"/></a><br/><b><a href="01-key-value-store/">01 · Key-Value Store</a></b></td>
<td width="50%" align="center"><a href="02-ad-creative-scorer/"><img src="02-ad-creative-scorer/docs/images/architecture.png" alt="02 Ad Creative Scorer architecture"/></a><br/><b><a href="02-ad-creative-scorer/">02 · Ad Creative Scorer</a></b></td>
</tr>
<tr>
<td width="50%" align="center"><a href="03-rate-limiter/"><img src="03-rate-limiter/docs/images/architecture.png" alt="03 Rate Limiter architecture"/></a><br/><b><a href="03-rate-limiter/">03 · Rate Limiter</a></b></td>
<td width="50%" align="center"><a href="04-consistent-hashing/"><img src="04-consistent-hashing/docs/images/architecture.png" alt="04 Consistent Hashing architecture"/></a><br/><b><a href="04-consistent-hashing/">04 · Consistent Hashing</a></b></td>
</tr>
</table>

## Portfolio map

Every entry below is a link, so you can jump straight to the file.

- 📁 **[aws-projects](./)**
  - 📘 [docs/CONVENTIONS.md](docs/CONVENTIONS.md): layout, README order, diagram and security rules
  - 🧩 [_template/](_template/): project skeleton · [scripts/new-project.sh](scripts/new-project.sh) scaffolds from it
  - 📦 **[01 · Key-Value Store](01-key-value-store/)**
    - [Architecture](01-key-value-store/docs/architecture.md) · [Workflows](01-key-value-store/docs/workflow.md)
    - [Template: kv-store.yaml](01-key-value-store/infrastructure/kv-store.yaml) · [Code: handler.py](01-key-value-store/src/handler.py)
    - [Integration test](01-key-value-store/tests/integration/test_api.py) · [Scripts](01-key-value-store/scripts/)
  - 📦 **[02 · Ad Creative Scorer](02-ad-creative-scorer/)**
    - [Architecture](02-ad-creative-scorer/docs/architecture.md) · [Workflows](02-ad-creative-scorer/docs/workflow.md)
    - [Templates: 01 to 06 stacks](02-ad-creative-scorer/infrastructure/) · [Code: 5 Lambdas](02-ad-creative-scorer/src/)
    - [Integration test](02-ad-creative-scorer/tests/integration/test_api.py) · [Scripts](02-ad-creative-scorer/scripts/)
  - 📦 **[03 · Rate Limiter](03-rate-limiter/)**
    - [Architecture](03-rate-limiter/docs/architecture.md) · [Workflows](03-rate-limiter/docs/workflow.md)
    - [Template](03-rate-limiter/infrastructure/template.yaml) · [Code](03-rate-limiter/src/) · [Scripts](03-rate-limiter/scripts/)
    - Algorithms: [Token bucket](03-rate-limiter/docs/token_bucket/README.md) · [Leaky bucket](03-rate-limiter/docs/leaky_bucket/README.md) · [Sliding window log](03-rate-limiter/docs/sliding_window_log/README.md) · [Fixed window counter](03-rate-limiter/docs/fixed_window_counter/README.md)
    - [Live tests](03-rate-limiter/tests/integration/)
  - 📦 **[04 · Consistent Hashing](04-consistent-hashing/)**
    - [Concepts, sections 1-8](04-consistent-hashing/docs/architecture.md) · [Workflows](04-consistent-hashing/docs/workflow.md)
    - KV store design: [Partitioning](04-consistent-hashing/docs/kv-1-partitioning.md) · [Replication](04-consistent-hashing/docs/kv-2-replication-consistency.md) · [Failures](04-consistent-hashing/docs/kv-3-failure-handling.md) · [System](04-consistent-hashing/docs/kv-4-system-architecture.md)
    - [Template](04-consistent-hashing/infrastructure/template.yaml) · [Code: ring handler](04-consistent-hashing/src/ring/handler.py) · [Code: kv coordinator](04-consistent-hashing/src/kv/handler.py) · [Scripts](04-consistent-hashing/scripts/)
    - [Integration test](04-consistent-hashing/tests/integration/test_api.py)

```mermaid
mindmap
  root((aws-projects))
    01 Key-Value Store
    02 Ad Creative Scorer
    03 Rate Limiter
      Middleware on EC2 + Redis
      Token bucket
      Leaky bucket · SQS
      Sliding window log
      Fixed window counter
    04 Consistent Hashing
      Rehashing problem
      Hash ring
      Add / remove servers
      Virtual nodes · DynamoDB
      Routing API · preference list
      Quorum N W R · replicas
      Vector clocks · LWW conflicts
      Sloppy quorum · hinted handoff
      Merkle-tree anti-entropy repair
      Simulated gossip failure detection
```

## How each project is built

```mermaid
flowchart LR
    A[Idea /<br/>requirements] --> B[Design<br/>README + diagrams]
    B --> C[Infrastructure<br/>CloudFormation]
    C --> D[Code<br/>src/]
    D --> E[Lint + package<br/>cfn-lint · cfn package]
    E --> F[Deploy<br/>scripts/deploy.sh]
    F --> G[Test<br/>tests/integration]
    G --> H[Document +<br/>teardown]
    H -.feedback.-> B
```

## Repository layout

```text
aws-projects/
├── README.md                    ← you are here: portfolio summary
├── 01-key-value-store/
│   ├── README.md                ← summary, diagrams, how to run
│   ├── docs/                    ← architecture.md, workflow.md, diagram.py, images/
│   ├── infrastructure/          ← kv-store.yaml
│   ├── src/                     ← handler.py
│   ├── tests/integration/       ← test_api.py
│   └── scripts/                 ← deploy.sh, destroy.sh
├── 02-ad-creative-scorer/
│   ├── README.md
│   ├── docs/
│   ├── infrastructure/          ← 01-foundation … 06-monitoring (deploy in order)
│   ├── src/                     ← one folder per Lambda
│   ├── tests/integration/
│   └── scripts/                 ← deploy.sh, train.sh, destroy.sh
├── 03-rate-limiter/
│   ├── README.md                ← summary, latency log
│   ├── docs/                    ← architecture.md, workflow.md, one folder per algorithm
│   ├── infrastructure/          ← template.yaml (Algorithm parameter)
│   ├── src/                     ← token_bucket/, leaky_bucket/, sliding_window_log/, fixed_window_counter/, backend/
│   ├── tests/integration/       ← latency.py, leaky_latency.py, window_latency.py
│   └── scripts/                 ← deploy.sh, destroy.sh
├── 04-consistent-hashing/
│   ├── README.md                ← summary, ring API, design-doc index
│   ├── docs/                    ← architecture.md, kv-1…kv-4, workflow.md, diagram*.py, images/
│   ├── infrastructure/          ← template.yaml
│   ├── src/                     ← ring/, kv/, shared/ring_math.py
│   ├── tests/integration/       ← test_api.py
│   └── scripts/                 ← deploy.sh, destroy.sh
├── docs/
│   └── CONVENTIONS.md           ← folder rules, README order, diagram + security standards
├── _template/                   ← project skeleton with {{NN}} {{slug}} {{Title}} placeholders
├── scripts/
│   └── new-project.sh           ← scaffold: ./scripts/new-project.sh 04 slug "Title"
└── .kiro/steering/              ← keeps Kiro/MCP following these conventions
```

## Documentation map

How the markdown files link together. Start at the top.

```mermaid
graph TD
    R[README.md<br/>portfolio] --> C[docs/CONVENTIONS.md]
    R --> T[_template/]
    R --> P1[01 README]
    R --> P2[02 README]
    R --> P3[03 README]
    P1 --> A1[architecture.md] & W1[workflow.md]
    P2 --> A2[architecture.md] & W2[workflow.md]
    A1 --> I1[infrastructure/*.yaml]
    A2 --> I2[infrastructure/01…06.yaml]
    W1 --> S1[scripts + tests]
    W2 --> S2[scripts + tests]
    P3 --> A3[architecture.md] & W3[workflow.md]
    A3 --> TB[token_bucket/README.md]
    A3 --> LB[leaky_bucket/README.md]
    A3 --> SW[sliding_window_log/README.md]
    A3 --> FW[fixed_window_counter/README.md]
    TB --> TBC[CODE_STRUCTURE.md]
    LB --> LBC[CODE_STRUCTURE.md]
    SW --> SWC[CODE_STRUCTURE.md]
    FW --> FWC[CODE_STRUCTURE.md]
    A3 --> I3[infrastructure/template.yaml]
    W3 --> S3[scripts + tests]
    R --> P4[04 README]
    P4 --> A4[architecture.md] & W4[workflow.md]
    P4 --> KV[kv-1 … kv-4 design docs]
    A4 --> I4[infrastructure/template.yaml]
    W4 --> S4[scripts + tests]
```

## Reading guide (for reviewers)

1. Look at the architecture images above. Click one to open that project.
2. In a project README, the first screen covers what it is, why it exists, and how it works.
3. `docs/architecture.md` has the design decisions. `docs/workflow.md` has step-by-step flows.
4. `infrastructure/` is the source of truth for what gets deployed.

## Prerequisites (all projects)

- AWS account + AWS CLI v2 configured (`aws sts get-caller-identity` works)
- Python 3.12+ and `boto3` for tests
- `cfn-lint` (`pip install cfn-lint`) to validate templates
- Optional, for regenerating diagrams: `pip install diagrams` + `brew install graphviz`

## Starting a new project

```bash
./scripts/new-project.sh 04 url-shortener "URL Shortener"
```

This copies [`_template/`](_template/) to `04-url-shortener/` and fills in the number, slug, and title everywhere. Then build the project, run `python3 docs/diagram.py`, add it to the table, gallery, map, and tree above, and follow [docs/CONVENTIONS.md](docs/CONVENTIONS.md).
