#!/bin/bash
# Create the deployment package for AgentCore Runtime (direct code deploy).
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html
#
# Always starts from a clean slate so stale dependencies never leak into the zip.
# The python version must match AGENT_PYTHON_RUNTIME in agentcore/agentcore_deployment.py.
set -euo pipefail

cd "$(dirname "$0")/agent_files"

rm -rf deployment_package deployment_package.zip

# Install dependencies for the ARM64 runtime platform
uv pip install \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --target=deployment_package \
  --only-binary=:all: \
  -r requirements.txt

# Normalize permissions (the runtime user must be able to read everything)
chmod -R a+rX deployment_package

# Create ZIP with dependencies (-X drops extra file attributes)
(
  cd deployment_package
  zip -q -r -X ../deployment_package.zip . -x "*/__pycache__/*" "*.pyc"
)

# Add source files to ZIP root (tests live outside agent_files and are not packaged)
chmod a+r ./*.py requirements.txt
zip -q -X deployment_package.zip ./*.py requirements.txt

echo "Deployment package created: agent_files/deployment_package.zip"
