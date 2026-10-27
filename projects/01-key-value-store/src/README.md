# src

The Lambda handler is currently **inline** in [`../infrastructure/kv-store.yaml`](../infrastructure/kv-store.yaml) (`KvFunction.Properties.Code.ZipFile`) so the stack deploys from one file with no S3 bucket.

When the handler grows past the inline limit (4 KB) or needs dependencies, move it here as `src/handler.py`, set `Code: src/` in the template, and deploy via `aws cloudformation package`.
