/**
 * Firmware Release Publisher
 * 
 * Handles publishing firmware bundles after the code-signing key rotation.
 * The old publisher was using a revoked key, so this one:
 * - Reads and reconciles the build manifest (removes duplicates, handles withdrawals)
 * - Signs bundles with the current key using OpenSSL
 * - Submits to the distribution gateway
 * - Stores receipts so we don't publish the same bundle twice
 * - Prints status in a deterministic order
 */

'use strict';

import duckdb from 'duckdb';
import fs from 'fs';
import { execFileSync } from 'child_process';
import os from 'os';
import path from 'path';

// ============================================================================
// Configuration
// ============================================================================

const GATEWAY_URL = process.env.GATEWAY_URL || 'http://127.0.0.1:7070';
const DB_PATH = '/app/releases.duckdb';
const MANIFEST_PATH = '/app/fixtures/build_manifest.csv';
const CURRENT_KEY_PATH = '/app/keys/current/current.key.pem';
const CURRENT_CERT_PATH = '/app/keys/current/current.cert.pem';

// ============================================================================
// Utility Functions
// ============================================================================

// Canonical JSON: sorted keys, no whitespace. Needed so signer and verifier
// agree on the exact bytes being signed.
function canonicalEncode(value) {
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalEncode).join(',') + ']';
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.keys(value)
      .sort()
      .map((k) => JSON.stringify(k) + ':' + canonicalEncode(value[k]));
    return '{' + entries.join(',') + '}';
  }
  return JSON.stringify(value);
}

// ============================================================================
// Gateway Integration
// ============================================================================

// Get the current signing key info from the gateway. We need to use this key
// for signing, otherwise the gateway will reject our signatures.
async function fetchSigningKeyMetadata() {
  const response = await fetch(`${GATEWAY_URL}/v1/signing-key/current`);
  if (!response.ok) {
    throw new Error(`Failed to fetch signing key: ${response.status}`);
  }
  return await response.json();
}

// Create a detached CMS signature using OpenSSL. We write the descriptor
// to a temp file, sign it, then read back the signature and clean up.
function createSignature(descriptorBytes) {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'sign-'));
  const descriptorFile = path.join(scratch, 'descriptor.bin');
  const signatureFile = path.join(scratch, 'signature.pem');

  try {
    fs.writeFileSync(descriptorFile, descriptorBytes);
    
    execFileSync(
      'openssl',
      [
        'cms',
        '-sign',
        '-in', descriptorFile,
        '-signer', CURRENT_CERT_PATH,
        '-inkey', CURRENT_KEY_PATH,
        '-outform', 'PEM',
        '-binary',
        '-out', signatureFile,
      ],
      { stdio: ['ignore', 'ignore', 'pipe'] }
    );
    
    return fs.readFileSync(signatureFile, 'utf8');
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

// Send the signed descriptor to the gateway. It verifies the signature
// and returns a receipt if everything checks out.
async function submitPublication(descriptor, signature, requestToken) {
  const response = await fetch(`${GATEWAY_URL}/v1/publications`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      descriptor,
      signature,
      request_token: requestToken,
    }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(`Publication failed: ${JSON.stringify(error)}`);
  }

  return await response.json();
}

// ============================================================================
// Database Operations
// ============================================================================

// Set up the database tables. One for the raw manifest data, one for
// tracking which bundles we've already published.
function initializeDatabase(db) {
  return new Promise((resolve, reject) => {
    db.run(`
      CREATE TABLE IF NOT EXISTS manifest (
        entry_id VARCHAR,
        bundle_id VARCHAR,
        component_id VARCHAR,
        version VARCHAR,
        size_bytes BIGINT,
        record_type VARCHAR,
        supersedes_id VARCHAR,
        recorded_at VARCHAR
      )
    `, (err) => {
      if (err) reject(err);
      else {
        db.run(`
          CREATE TABLE IF NOT EXISTS publications (
            bundle_id VARCHAR PRIMARY KEY,
            request_token VARCHAR,
            publication_id VARCHAR,
            status VARCHAR,
            descriptor VARCHAR,
            signature VARCHAR
          )
        `, (err2) => {
          if (err2) reject(err2);
          else resolve();
        });
      }
    });
  });
}

// Load the CSV manifest into the database so we can query it.
// Guard against double-loading on re-runs: only import if the table is empty.
function loadManifest(db) {
  return new Promise((resolve, reject) => {
    db.all('SELECT COUNT(*) AS cnt FROM manifest', (err, rows) => {
      if (err) return reject(err);
      const count = Number(rows[0].cnt);
      if (count > 0) {
        // Already loaded from a previous run — skip to avoid duplicate rows.
        return resolve();
      }
      db.run(`
        COPY manifest FROM '${MANIFEST_PATH}' (HEADER, DELIMITER ',')
      `, (err2) => {
        if (err2) reject(err2);
        else resolve();
      });
    });
  });
}

// Figure out which bundles should actually be published.
// - Remove duplicate rows
// - Exclude builds that were withdrawn
// - Count surviving builds and total bytes per bundle
// - Only return bundles that have at least one surviving build
function getPublishableBundles(db) {
  return new Promise((resolve, reject) => {
    const query = `
      WITH deduplicated AS (
        SELECT DISTINCT *
        FROM manifest
      ),
      withdrawn_entry_ids AS (
        SELECT supersedes_id
        FROM deduplicated
        WHERE record_type = 'WITHDRAWAL'
      ),
      surviving_builds AS (
        SELECT *
        FROM deduplicated
        WHERE record_type = 'BUILD'
          AND entry_id NOT IN (SELECT supersedes_id FROM withdrawn_entry_ids)
      ),
      bundle_stats AS (
        SELECT 
          bundle_id,
          COUNT(*) as artifact_count,
          SUM(size_bytes) as total_bytes
        FROM surviving_builds
        GROUP BY bundle_id
        HAVING COUNT(*) > 0
      )
      SELECT bundle_id, artifact_count, total_bytes
      FROM bundle_stats
      ORDER BY bundle_id ASC
    `;

    db.all(query, (err, rows) => {
      if (err) reject(err);
      else resolve(rows || []);
    });
  });
}

// Check if we've already published this bundle. If so, we can just
// replay the receipt instead of submitting again.
function getExistingPublication(db, bundleId) {
  return new Promise((resolve, reject) => {
    db.all(`
      SELECT request_token, publication_id, status, descriptor, signature
      FROM publications
      WHERE bundle_id = ?
    `, [bundleId], (err, rows) => {
      if (err) reject(err);
      else resolve(rows && rows.length > 0 ? rows[0] : null);
    });
  });
}

// Save the publication receipt so we know we've already published this bundle.
// INSERT OR REPLACE handles the case where we're re-running the publisher.
function storePublication(db, bundleId, requestToken, publicationId, status, descriptor, signature) {
  return new Promise((resolve, reject) => {
    const escapedDescriptor = descriptor.replace(/'/g, "''");
    const escapedSignature = signature.replace(/'/g, "''");
    db.run(`
      INSERT OR REPLACE INTO publications 
      (bundle_id, request_token, publication_id, status, descriptor, signature)
      VALUES ('${bundleId}', '${requestToken}', '${publicationId}', '${status}', '${escapedDescriptor}', '${escapedSignature}')
    `, (err) => {
      if (err) reject(err);
      else resolve();
    });
  });
}

// ============================================================================
// Main Execution
// ============================================================================

async function main() {
  const isReportMode = process.argv.includes('--report');

  // Set up the database and load the manifest
  const db = new duckdb.Database(DB_PATH);
  await initializeDatabase(db);
  await loadManifest(db);

  // Get the current signing key info from the gateway
  const keyMetadata = await fetchSigningKeyMetadata();
  const { key_id: keyId } = keyMetadata;

  // Figure out which bundles we need to publish
  const bundles = await getPublishableBundles(db);

  // Process each bundle in order
  for (const bundle of bundles) {
    const { bundle_id: bundleId, artifact_count: artifactCount, total_bytes: totalBytes } = bundle;
    const requestToken = `token-${bundleId}`;

    // DuckDB returns BigInt, convert to regular Number for JSON
    const artifactCountNum = Number(artifactCount);
    const totalBytesNum = Number(totalBytes);

    // Check if we already published this one
    const existing = await getExistingPublication(db, bundleId);

    if (existing) {
      // Already published, just print the receipt
      console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);
      console.log(`BUNDLE ${bundleId} PUBLISHED RECEIPT=${existing.publication_id} TOKEN=${existing.request_token} STATUS=${existing.status}`);
      continue;
    }

    // Create the descriptor, sign it, and submit
    const descriptor = canonicalEncode({
      bundle_id: bundleId,
      artifact_count: artifactCountNum,
      total_bytes: totalBytesNum,
    });
    const descriptorBytes = Buffer.from(descriptor, 'utf8');

    const signature = createSignature(descriptorBytes);

    console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);

    const receipt = await submitPublication(descriptor, signature, requestToken);

    // Save the receipt so we don't publish this again
    await storePublication(
      db,
      bundleId,
      receipt.request_token,
      receipt.publication_id,
      receipt.status,
      descriptor,
      signature
    );

    console.log(`BUNDLE ${bundleId} PUBLISHED RECEIPT=${receipt.publication_id} TOKEN=${receipt.request_token} STATUS=${receipt.status}`);
  }

  db.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
