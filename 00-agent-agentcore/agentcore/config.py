"""Shared configuration helpers for the AgentCore stack.

Keeps the default model, memory namespaces and ARN helpers in one place so the
CDK constructs, the runtime environment variables and the unit tests agree.
"""

from typing import List, Optional

# Single source of truth for the default Bedrock model. Claude Sonnet 4.6 is
# active, so the previous Sonnet 4 default (EOL 2026-10-14) is no longer used.
# Opt in to another model with `MODEL_ID` (env) or `-c model_id=...` (CDK context).
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"

# Geographic prefixes used by Bedrock cross-Region inference profiles.
_INFERENCE_PROFILE_PREFIXES = ("global", "us", "eu", "apac", "jp", "au", "ca", "us-gov")

# Memory namespaces. The agent code (agent_files/multimodal_agent.py) holds the
# same templates; tests/test_namespaces.py asserts both copies stay identical.
FACTS_NAMESPACE = "/facts/{actorId}/"
PREFERENCES_NAMESPACE = "/preferences/{actorId}/"
SUMMARIES_NAMESPACE = "/summaries/{actorId}/{sessionId}/"
EPISODES_NAMESPACE = "/episodes/{actorId}/"

# Short-term event retention. Long-term records are stored separately.
DEFAULT_MEMORY_EXPIRY_DAYS = 30

# Runtime name prefix used to scope log group permissions.
RUNTIME_NAME = "WhatsAppMultimodalAgentV2"


def is_inference_profile(model_id: str) -> bool:
    """True when model_id looks like a cross-Region inference profile ID."""
    return model_id.split(".", 1)[0] in _INFERENCE_PROFILE_PREFIXES and "." in model_id


def foundation_model_id(model_id: str) -> str:
    """Return the base foundation model ID behind an inference profile ID."""
    if is_inference_profile(model_id):
        return model_id.split(".", 1)[1]
    return model_id


def model_resource_arns(model_id: str, region: str, account: str, partition: str = "aws") -> List[str]:
    """ARNs needed to invoke ``model_id``.

    An inference profile needs the profile ARN (in the caller's Region and
    account) plus the foundation model in every Region the profile can route to,
    hence the Region wildcard on the foundation model ARN. This is what the
    Bedrock documentation requires for cross-Region inference.
    """
    arns = [f"arn:{partition}:bedrock:*::foundation-model/{foundation_model_id(model_id)}"]
    if is_inference_profile(model_id):
        arns.append(f"arn:{partition}:bedrock:{region}:{account}:inference-profile/{model_id}")
    return arns


def guardrail_arn(guardrail_id: Optional[str], region: str, account: str, partition: str = "aws") -> Optional[str]:
    if not guardrail_id:
        return None
    return f"arn:{partition}:bedrock:{region}:{account}:guardrail/{guardrail_id}"
