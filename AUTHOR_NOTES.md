# Author Notes - Firmware Release Publisher

## Solution Overview
This solution implements a firmware release publisher that addresses the code-signing key rotation issue. The publisher reconciles build manifest data using DuckDB SQL, signs bundles with the current key using OpenSSL CMS, submits to the distribution gateway, and persists receipts for idempotency.

## Implementation Details

### Data Reconciliation (DuckDB SQL)
The reconciliation logic uses a CTE-based SQL query to:
1. Remove exact duplicate rows using `SELECT DISTINCT`
2. Identify withdrawn builds via `WITHDRAWAL` records
3. Filter out builds referenced in withdrawals
4. Aggregate surviving builds per bundle (count and total bytes)
5. Return only bundles with surviving builds

### Signature Generation (OpenSSL CMS)
- Uses canonical JSON encoding (sorted keys, no whitespace)
- Creates detached CMS signatures with the current keypair
- Temporarily writes descriptor to file for OpenSSL processing
- Cleans up temporary files after signing

### Gateway Integration
- Fetches current signing key metadata from `GET /v1/signing-key/current`
- Submits signed descriptors to `POST /v1/publications`
- Uses deterministic request tokens: `token-<bundle_id>`
- Handles idempotency via gateway's token replay mechanism

### Persistence (DuckDB)
- Stores publication receipts in `publications` table
- Includes: bundle_id, request_token, publication_id, status, descriptor, signature
- Uses `INSERT OR REPLACE` for idempotent updates
- Enables replay of existing receipts on re-runs

### Deterministic Output
- Processes bundles in ascending `bundle_id` order
- Emits exactly two lines per bundle (SIGNED, PUBLISHED)
- Uses current key_id from gateway metadata

## Testing Instructions

### Build the Docker image
```bash
cd environment
docker build -t firmware-publisher .
```

### Terminal 1: Start the distribution gateway
```bash
docker run -it --rm -p 7070:7070 firmware-publisher bash
cd /app/distribution-gateway && node server.js
```

### Terminal 2: Test the solution
```bash
docker run -it --rm --network host firmware-publisher bash
cd /app && npm run report
```

### Verify idempotency
```bash
npm run report > /tmp/a.txt
npm run report > /tmp/b.txt
diff /tmp/a.txt /tmp/b.txt  # Should be empty
```

## Key Design Decisions

1. **SQL-based reconciliation**: Using DuckDB CTEs provides clear, maintainable logic for data reconciliation
2. **Canonical JSON encoding**: Ensures signer and verifier agree on exact byte representation
3. **Idempotency via database**: Storing receipts allows re-runs without duplicate submissions
4. **Deterministic ordering**: Sorting by bundle_id ensures reproducible output
5. **Current key fetching**: Dynamic key lookup ensures solution works with future key rotations

## Known Limitations
- The Dockerfile currently copies the solution from solution/ to environment/publisher/ at build time. For the empty-run proof (Proof A), environment/publisher/ must be empty.
