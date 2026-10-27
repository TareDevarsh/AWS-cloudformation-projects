# Conventions

Rules every project follows so the repo reads the same everywhere.

## Folder naming

- `projects/NN-short-name/` — two-digit number gives reading order (`01-key-value-store`, `02-url-shortener`).
- Lowercase, hyphen-separated.

## Required files per project

| Path | Purpose |
|------|---------|
| `README.md` | What / why / how, architecture diagram, deploy + test + teardown steps |
| `docs/architecture.md` | Components, data model, design decisions and trade-offs |
| `docs/workflow.md` | Sequence diagrams for main flows, deploy lifecycle |
| `infrastructure/*.yaml` | CloudFormation template(s); source of truth for deployed resources |
| `src/` | Application code (or a README pointing to where it lives) |
| `scripts/` | `deploy.sh`, `destroy.sh`, test script |

## Project README section order

1. **Summary** — one paragraph + key facts table
2. **Architecture** — Mermaid diagram
3. **How it works** — short request flow
4. **Repo layout** — tree
5. **Deploy / Test / Teardown** — copy-paste commands
6. **Cost & security notes**
7. **What I learned / next steps**

## Diagrams

- Use **Mermaid** in markdown (renders natively on GitHub/GitLab).
- `flowchart` for architecture, `sequenceDiagram` for request flows, `stateDiagram` for lifecycles, `erDiagram` or tables for data models.
- Tree layouts use plain `text` code blocks.

## CloudFormation

- YAML, one stack per project unless there's a clear reason to split.
- Parameters for anything environment-specific (`Stage`, names).
- Tag everything with `Project` and `Stage`.
- Outputs for every value a script or reviewer needs (endpoints, table names).
- Validate before deploy: `cfn-lint infrastructure/*.yaml` and `aws cloudformation validate-template`.

## Security defaults

- APIs require authentication (IAM/SigV4 by default). No anonymous endpoints unless the project is explicitly public.
- IAM roles are least-privilege, scoped to the project's own resources.
- Storage encrypted at rest; S3 Block Public Access on; no `0.0.0.0/0` ingress to databases.
- No secrets in the repo. Use Secrets Manager / SSM Parameter Store.

## Git

- Each project is a folder in this one repo; no nested git repos.
- Never commit build output, `.env`, or credentials (see `.gitignore`).
