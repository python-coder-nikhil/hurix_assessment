"""Verifier tests for the Firmware Release Publisher task.

Each test maps to a functional_criteria[] entry in scaffold_plan.yaml.
The tests start the distribution gateway, run `npm run report`, and verify:
  - stdout matches the golden output (RECEIPT masked)
  - reconciliation is correct (BND-104 excluded, duplicates collapsed)
  - all bundles are PUBLISHED (no UNTRUSTED_SIGNATURE)
  - receipts and tokens are persisted in releases.duckdb
  - re-running is idempotent (same output, no duplicate gateway publications)
  - a revoked-key signature is rejected by the gateway

Run via tests/test.sh, which writes /logs/verifier/reward.txt.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import json
from pathlib import Path

import duckdb
import requests

APP_DIR = Path("/app")
GATEWAY_URL = "http://127.0.0.1:7070"
EXPECTED_FILE = APP_DIR / "reports" / "publications.expected.txt"
DUCKDB_PATH = APP_DIR / "releases.duckdb"
MANIFEST_PATH = APP_DIR / "fixtures" / "build_manifest.csv"
GATEWAY_DIR = APP_DIR / "distribution-gateway"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mask_receipts(text: str) -> str:
    """Replace receipt IDs (RECEIPT=<anything up to the next space>) with a
    placeholder so the diff is not sensitive to the randomised publication_id."""
    return re.sub(r"RECEIPT=\S+", "RECEIPT=<id>", text)


def wait_for_gateway(timeout: int = 30) -> None:
    """Block until the gateway /healthz returns 200, or raise if it never does."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{GATEWAY_URL}/healthz", timeout=2)
            if r.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Gateway at {GATEWAY_URL} did not become ready within {timeout}s")


def run_report() -> subprocess.CompletedProcess:
    """Execute `npm run report` in /app and return the CompletedProcess.
    Strips npm's own header lines ("> script-name" / "> command") so callers
    see only the publisher's stdout."""
    result = subprocess.run(
        ["npm", "run", "report"],
        capture_output=True,
        text=True,
        cwd=str(APP_DIR),
    )
    # npm prepends lines like:
    #   > release-publisher@1.0.0 report
    #   > node publisher/release-publisher.mjs --report
    # followed by a blank line. Strip them so comparisons work against the
    # golden file which contains only the publisher's own output.
    lines = result.stdout.splitlines(keepends=True)
    clean_lines = []
    skip_next_blank = False
    for line in lines:
        if line.startswith("> "):
            skip_next_blank = True
            continue
        if skip_next_blank and line.strip() == "":
            skip_next_blank = False
            continue
        skip_next_blank = False
        clean_lines.append(line)
    result = subprocess.CompletedProcess(
        result.args, result.returncode,
        stdout="".join(clean_lines), stderr=result.stderr
    )
    return result


# ---------------------------------------------------------------------------
# functional_criteria[id=report_output_matches]
# ---------------------------------------------------------------------------

def test_report_output_matches_golden():
    """Running `npm run report` produces output that matches the golden file
    (with RECEIPT values masked)."""
    wait_for_gateway()
    result = run_report()
    assert result.returncode == 0, (
        f"npm run report exited {result.returncode}.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    expected_raw = EXPECTED_FILE.read_text(encoding="utf-8")
    expected_masked = mask_receipts(expected_raw).strip()
    actual_masked = mask_receipts(result.stdout).strip()

    assert actual_masked == expected_masked, (
        f"Output does not match golden.\n"
        f"Expected (masked):\n{expected_masked}\n\n"
        f"Actual (masked):\n{actual_masked}"
    )


# ---------------------------------------------------------------------------
# functional_criteria[id=withdrawals_and_duplicates_reconciled]
# ---------------------------------------------------------------------------

def test_withdrawals_and_duplicates_reconciled():
    """BND-104 is fully withdrawn and must not appear in the output.
    Duplicate manifest rows must be collapsed. BND-101, BND-102, BND-103
    must be present."""
    wait_for_gateway()
    result = run_report()
    assert result.returncode == 0, result.stderr

    output = result.stdout
    # Fully-withdrawn bundle must be absent
    assert "BND-104" not in output, (
        "BND-104 is fully withdrawn and must not appear in the output."
    )
    # Publishable bundles must be present
    for bundle in ("BND-101", "BND-102", "BND-103"):
        assert bundle in output, f"{bundle} is publishable and must appear in the output."


# ---------------------------------------------------------------------------
# functional_criteria[id=bundles_signed_with_current_key_accepted]
# ---------------------------------------------------------------------------

def test_bundles_signed_with_current_key_accepted():
    """All bundles in the output must carry STATUS=PUBLISHED; none may show
    UNTRUSTED_SIGNATURE, proving the publisher used the current key."""
    wait_for_gateway()
    result = run_report()
    assert result.returncode == 0, result.stderr

    assert "UNTRUSTED_SIGNATURE" not in result.stdout, (
        "At least one bundle was rejected with UNTRUSTED_SIGNATURE — the publisher"
        " signed with the wrong (revoked) key."
    )
    published_lines = [l for l in result.stdout.splitlines() if "STATUS=PUBLISHED" in l]
    assert len(published_lines) == 3, (
        f"Expected 3 PUBLISHED lines (BND-101, BND-102, BND-103), got {len(published_lines)}."
    )


# ---------------------------------------------------------------------------
# functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]
# ---------------------------------------------------------------------------

def test_receipts_and_tokens_persisted_in_duckdb():
    """After a run, releases.duckdb must exist and contain the publication
    receipts and request tokens for each submitted bundle."""
    wait_for_gateway()
    result = run_report()
    assert result.returncode == 0, result.stderr

    assert DUCKDB_PATH.exists(), "releases.duckdb was not created by the publisher."

    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        rows = con.execute(
            "SELECT bundle_id, request_token, publication_id, status FROM publications ORDER BY bundle_id"
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == 3, (
        f"Expected 3 rows in releases.duckdb publications table, got {len(rows)}.\nRows: {rows}"
    )

    bundle_ids = {r[0] for r in rows}
    assert bundle_ids == {"BND-101", "BND-102", "BND-103"}, (
        f"Unexpected bundle_ids in DB: {bundle_ids}"
    )

    for bundle_id, request_token, publication_id, status in rows:
        assert request_token == f"token-{bundle_id}", (
            f"Expected token-{bundle_id}, got {request_token}"
        )
        assert status == "PUBLISHED", f"Expected PUBLISHED status, got {status} for {bundle_id}"
        assert publication_id, f"publication_id is empty for {bundle_id}"


# ---------------------------------------------------------------------------
# functional_criteria[id=idempotent_rerun_no_duplicate_publications]
# ---------------------------------------------------------------------------

def test_idempotent_rerun_no_duplicate_publications():
    """Running the publisher twice must produce byte-identical output and must
    not create duplicate publications on the gateway."""
    wait_for_gateway()

    # First run (may already have been done by earlier tests, that is fine)
    r1 = run_report()
    assert r1.returncode == 0, f"First run failed: {r1.stderr}"

    # Second run
    r2 = run_report()
    assert r2.returncode == 0, f"Second run failed: {r2.stderr}"

    assert mask_receipts(r1.stdout) == mask_receipts(r2.stdout), (
        f"Output is not identical across two runs.\nRun 1:\n{r1.stdout}\nRun 2:\n{r2.stdout}"
    )

    # The gateway must still have exactly 3 publications (not 6)
    r = requests.get(f"{GATEWAY_URL}/healthz", timeout=5)
    assert r.status_code == 200

    # Verify via DuckDB that we still have exactly 3 rows (no duplicates inserted)
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        count = con.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    finally:
        con.close()
    assert count == 3, (
        f"Expected exactly 3 publications in DuckDB after two runs, got {count}."
    )


# ---------------------------------------------------------------------------
# functional_criteria[id=revoked_key_signature_rejected]
# ---------------------------------------------------------------------------

def test_revoked_key_signature_rejected():
    """A descriptor signed with the revoked key must be rejected by the gateway
    with UNTRUSTED_SIGNATURE. This confirms signature verification is real."""
    wait_for_gateway()

    import tempfile, os as _os
    from subprocess import run as _run

    descriptor = json.dumps(
        {"artifact_count": 1, "bundle_id": "BND-TEST", "total_bytes": 100},
        sort_keys=True,
        separators=(",", ":"),
    )

    revoked_key = "/app/keys/revoked/revoked.key.pem"
    revoked_cert = "/app/keys/revoked/revoked.cert.pem"

    with tempfile.TemporaryDirectory() as tmp:
        desc_file = _os.path.join(tmp, "descriptor.bin")
        sig_file = _os.path.join(tmp, "sig.pem")

        Path(desc_file).write_bytes(descriptor.encode("utf-8"))

        sign_result = _run(
            [
                "openssl", "cms", "-sign",
                "-in", desc_file,
                "-signer", revoked_cert,
                "-inkey", revoked_key,
                "-outform", "PEM",
                "-binary",
                "-out", sig_file,
            ],
            capture_output=True,
        )
        assert sign_result.returncode == 0, (
            f"openssl signing with revoked key failed: {sign_result.stderr.decode()}"
        )
        signature = Path(sig_file).read_text(encoding="utf-8")

    response = requests.post(
        f"{GATEWAY_URL}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": signature,
            "request_token": "token-revoked-test",
        },
        timeout=10,
    )

    assert response.status_code != 200, (
        "Gateway accepted a revoked-key signature — verification is not working."
    )
    body = response.json()
    assert body.get("error") == "UNTRUSTED_SIGNATURE", (
        f"Expected UNTRUSTED_SIGNATURE error, got: {body}"
    )
