#!/bin/bash

# Create deployment package for AgentCore Runtime as per AWS documentation
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html

cd agent_files || exit 1

# Install dependencies for ARM64 architecture
uv pip install \
--python-platform aarch64-manylinux2014 \
--python-version 3.11 \
--target=deployment_package \
--only-binary=:all: \
-r requirements.txt

# Create ZIP with dependencies
(
  cd deployment_package || exit 1
  zip -r ../deployment_package.zip .
)

# Add source files to ZIP root
zip deployment_package.zip ./*.py requirements.txt

echo "Deployment package created: agent_files/deployment_package.zip"
