#!/bin/bash

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile before running this script."
    exit 1
fi

mkdir -p /logs/verifier

# ---------------------------------------------------------------------------
# Start the distribution gateway in the background.
# The verifier owns this step; the candidate's publish.sh only deploys
# the publisher module — it does NOT start the gateway.
# ---------------------------------------------------------------------------
echo "==> Starting distribution gateway ..."
node /app/distribution-gateway/server.js &
GATEWAY_PID=$!

# Wait up to 15 seconds for the gateway to be ready on port 7070.
# Uses Python (always available in this image) instead of curl.
echo "==> Waiting for gateway on port 7070 ..."
python3 - <<'PYEOF'
import sys, time, urllib.request, urllib.error
deadline = time.time() + 15
while time.time() < deadline:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:7070/healthz", timeout=2)
        if r.status == 200:
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.5)
sys.exit(1)
PYEOF

if [ $? -ne 0 ]; then
    echo "ERROR: Gateway did not start within 15 seconds."
    kill "$GATEWAY_PID" 2>/dev/null
    echo 0 > /logs/verifier/reward.txt
    exit 1
fi
echo "==> Gateway is ready."

# ---------------------------------------------------------------------------
# Run the verifier test suite.
# pytest + pytest-json-ctrf are pre-installed in the verifier image (shared
# mode). allow_internet=false, so no wheels are resolved at run time.
# ---------------------------------------------------------------------------
python3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
code=$?

# Shut down the gateway cleanly.
kill "$GATEWAY_PID" 2>/dev/null

# Surface pytest's raw exit code so the negative-control check can tell
# "tests ran and failed" (code 1, expected with no solution) from
# "tests could not run" (>=2).
echo "pytest exit code: ${code}"

if [ "$code" -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
