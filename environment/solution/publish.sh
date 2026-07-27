#!/bin/bash
# Solution entrypoint for Firmware Release Publisher
# Copies the reference implementation to the environment directory

set -e

# Create the publisher directory if it doesn't exist
mkdir -p /app/publisher

# Copy the solution to the environment directory
cp /app/solution/release-publisher.mjs /app/publisher/release-publisher.mjs
