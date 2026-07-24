# Firmware Release Publisher

## Problem
The Release Engineering team rotated the firmware code-signing key and revoked the previous signing certificate. The legacy publisher continues to sign artifacts with the revoked key, causing all release bundles to be rejected by the distribution gateway with `UNTRUSTED_SIGNATURE` errors.

## Task
Implement a new publisher at `/app/publisher/release-publisher.mjs` that:

1. Reads and reconciles `/app/fixtures/build_manifest.csv` using DuckDB SQL
2. Signs each publishable bundle with the current key at `/app/keys/current/` using OpenSSL CMS
3. Submits signed bundles to the Express gateway at `http://127.0.0.1:7070`
4. Persists receipts and idempotency tokens in `/app/releases.duckdb`
5. Emits deterministic status lines matching `/app/reports/publications.expected.txt`

## Reconciliation Rules
- Remove exact duplicate rows (identical across all columns)
- Exclude builds referenced by WITHDRAWAL records (via supersedes_id)
- A bundle is publishable only if it has at least one surviving build
- For each publishable bundle, calculate: artifact_count and total_bytes

## Output Format
For each publishable bundle (ordered by bundle_id ASC):
```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=PUBLISHED
```

## Success Conditions
- `npm run report` reproduces the golden output (receipt ID masked)
- All submissions return STATUS=PUBLISHED (no UNTRUSTED_SIGNATURE)
- Idempotency: re-running produces identical output
- Fully-withdrawn bundles are excluded
- Deterministic ordering by bundle_id

## Boundaries
- Interact with gateway only over HTTP (do not read/write `distribution-gateway/data/gateway.json`)
- Do not bypass signature verification
- Do not sign with the revoked key at `/app/keys/revoked/`
- Do not hardcode golden text, receipt IDs, or row counts
- Use deterministic request tokens: `token-<bundle_id>`
