#!/bin/bash
# Build the common Lambda layer for the Lambda runtime used by this stack
# (python3.13, arm64). Dependencies are resolved for aarch64 Linux wheels so the layer
# works no matter which OS runs `cdk deploy`.
# The Python version must match RUNTIME in lambdas/project_lambdas.py.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf .build
mkdir -p .build/python

uv pip install \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --target=.build/python \
  --only-binary=:all: \
  -r requirements.txt

# Shared source modules next to the dependencies
cp common/python/*.py .build/python/

chmod -R a+rX .build
find .build -name "__pycache__" -type d -prune -exec rm -rf {} +
echo "Layer built: layers/.build/python"
