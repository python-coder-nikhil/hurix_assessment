#!/bin/bash
# Solution entrypoint for Firmware Release Publisher
# Copies the reference implementation to the environment and runs it

set -e

# Create the publisher directory if it doesn't exist
mkdir -p /app/publisher

# Copy the solution to the environment directory
cp /app/solution/release-publisher.mjs /app/publisher/release-publisher.mjs

# Run the publisher
cd /app
npm run report
