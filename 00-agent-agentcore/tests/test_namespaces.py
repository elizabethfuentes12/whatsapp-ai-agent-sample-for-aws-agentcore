import agent_helpers
import multimodal_agent
from agentcore import config


def test_namespaces_match_between_cdk_and_agent():
    for name in ("FACTS_NAMESPACE", "PREFERENCES_NAMESPACE", "SUMMARIES_NAMESPACE", "EPISODES_NAMESPACE"):
        assert getattr(config, name) == getattr(agent_helpers, name)


def test_default_model_matches_between_cdk_and_agent():
    assert config.DEFAULT_MODEL_ID == multimodal_agent.DEFAULT_MODEL_ID


def test_model_arns_for_global_profile():
    arns = config.model_resource_arns("global.anthropic.claude-sonnet-4-6", "us-east-1", "123456789012")
    assert arns == [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-sonnet-4-6",
    ]


def test_model_arns_for_plain_foundation_model():
    assert config.model_resource_arns("anthropic.claude-sonnet-4-6", "eu-west-1", "1") == [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6"
    ]
    assert config.foundation_model_id("eu.anthropic.claude-sonnet-4-6") == "anthropic.claude-sonnet-4-6"
