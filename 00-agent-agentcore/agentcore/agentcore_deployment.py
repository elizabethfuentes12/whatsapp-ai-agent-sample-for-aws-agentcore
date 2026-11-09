"""AgentCore Runtime deployment construct using CfnRuntime."""

import hashlib
import os
import subprocess
from typing import Optional

from constructs import Construct
from aws_cdk import (
    aws_bedrockagentcore as bedrockagentcore,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_iam as iam,
)

from agentcore.config import RUNTIME_NAME

STACK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AGENT_FILES_DIR = os.path.join(STACK_DIR, "agent_files")
PACKAGE_SCRIPT = os.path.join(STACK_DIR, "create_deployment_package.sh")
ZIP_PATH = os.path.join(AGENT_FILES_DIR, "deployment_package.zip")
HASH_PATH = os.path.join(AGENT_FILES_DIR, "deployment_package.sha256")

# Python runtime for direct code deploy. 3.11 is deprecated for new/updated
# runtimes, 3.13 is supported until 2029. Keep in sync with the
# `--python-version` flag in create_deployment_package.sh.
AGENT_PYTHON_RUNTIME = "PYTHON_3_13"

# Custom header the Lambda can use to carry the actor ID. Only headers listed in
# the allowlist reach `context.request_headers` inside the agent.
ACTOR_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id"


def source_hash() -> str:
    """Hash of everything that goes into the deployment package."""
    digest = hashlib.sha256()
    names = sorted(
        n for n in os.listdir(AGENT_FILES_DIR) if n.endswith(".py") or n == "requirements.txt"
    )
    for name in names + [os.path.join("..", "create_deployment_package.sh")]:
        path = os.path.join(AGENT_FILES_DIR, name)
        digest.update(name.encode())
        with open(path, "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()


def ensure_deployment_package() -> str:
    """Return the zip path, rebuilding it whenever the sources changed."""
    current = source_hash()
    previous = None
    if os.path.exists(HASH_PATH):
        with open(HASH_PATH, encoding="utf-8") as handle:
            previous = handle.read().strip()

    if os.path.exists(ZIP_PATH) and previous == current:
        return ZIP_PATH

    result = subprocess.run(
        ["bash", PACKAGE_SCRIPT], cwd=STACK_DIR, capture_output=True, text=True
    )
    if result.returncode != 0 or not os.path.exists(ZIP_PATH):
        raise RuntimeError(
            "Failed to create the AgentCore deployment package. "
            "Check that `uv` is installed and has network access.\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
        )
    with open(HASH_PATH, "w", encoding="utf-8") as handle:
        handle.write(current)
    return ZIP_PATH


class AgentCoreDeployment(Construct):
    """Creates an AgentCore Runtime with code-based deployment.

    Args:
        package_path: Optional path to a prebuilt zip. When omitted the zip is
            built (or rebuilt when sources changed) by create_deployment_package.sh.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        bucket: s3.Bucket,
        role: iam.Role,
        memory_id: str = "",
        environment_variables: Optional[dict] = None,
        package_path: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_vars = {
            "S3_BUCKET": bucket.bucket_name,
        }
        if memory_id:
            env_vars["BEDROCK_AGENTCORE_MEMORY_ID"] = memory_id
        if environment_variables:
            env_vars.update(environment_variables)

        zip_path = package_path or ensure_deployment_package()

        code_asset = s3_assets.Asset(self, "AgentCodeAsset", path=zip_path)
        code_asset.grant_read(role)

        self.runtime = bedrockagentcore.CfnRuntime(
            self,
            "AgentCoreRuntime",
            agent_runtime_artifact=bedrockagentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=bedrockagentcore.CfnRuntime.CodeConfigurationProperty(
                    code=bedrockagentcore.CfnRuntime.CodeProperty(
                        s3=bedrockagentcore.CfnRuntime.S3LocationProperty(
                            bucket=code_asset.s3_bucket_name,
                            prefix=code_asset.s3_object_key,
                        )
                    ),
                    entry_point=["multimodal_agent.py"],
                    runtime=AGENT_PYTHON_RUNTIME,
                )
            ),
            agent_runtime_name=RUNTIME_NAME,
            description="Multimodal WhatsApp agent with AgentCore Memory",
            environment_variables=env_vars,
            # PUBLIC network mode with IAM (SigV4) auth: the runtime is only
            # invoked server to server by the message-processor Lambda, so there
            # is no end-user identity provider and no JWT authorizer to configure.
            # For private access to VPC resources use VPC network mode instead.
            network_configuration=bedrockagentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC"
            ),
            lifecycle_configuration=bedrockagentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=900,  # 15 minutes in seconds
                max_lifetime=28800,  # 8 hours in seconds
            ),
            request_header_configuration=bedrockagentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=[ACTOR_ID_HEADER],
            ),
            role_arn=role.role_arn,
        )
        self.runtime.node.add_dependency(code_asset)

    @property
    def agent_runtime_arn(self) -> str:
        return self.runtime.attr_agent_runtime_arn

    @property
    def agent_runtime_id(self) -> str:
        return self.runtime.ref
