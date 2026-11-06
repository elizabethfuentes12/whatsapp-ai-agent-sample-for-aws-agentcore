"""CDK assertion tests (offline, no AWS credentials)."""

import pytest
from aws_cdk import App, assertions

from agent_agentcore.agent_agentcore_stack import AgentAgentCoreStack
from agentcore import config


def synth(stub_zip, **context):
    app = App(context={"package_path": stub_zip, **context})
    stack = AgentAgentCoreStack(app, "TestStack")
    return assertions.Template.from_stack(stack)


@pytest.fixture
def template(stub_zip):
    return synth(stub_zip)


def policy_statements(template):
    statements = []
    for resource in template.find_resources("AWS::IAM::Policy").values():
        statements.extend(resource["Properties"]["PolicyDocument"]["Statement"])
    return statements


def test_runtime_uses_current_python_and_entrypoint(template):
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "AgentRuntimeArtifact": {
                "CodeConfiguration": assertions.Match.object_like(
                    {"Runtime": "PYTHON_3_13", "EntryPoint": ["multimodal_agent.py"]}
                )
            },
            "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
            "RequestHeaderConfiguration": {
                "RequestHeaderAllowlist": ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id"]
            },
        },
    )


def test_runtime_environment_has_current_model_and_no_strategy_ids(template):
    runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
    env = next(iter(runtimes.values()))["Properties"]["EnvironmentVariables"]
    assert env["MODEL_ID"] == config.DEFAULT_MODEL_ID
    assert "FACTS_STRATEGY_ID" not in env and "PREFERENCES_STRATEGY_ID" not in env
    assert "BEDROCK_GUARDRAIL_ID" not in env
    assert "BEDROCK_AGENTCORE_MEMORY_ID" in env


def test_memory_strategies_have_namespaces_and_30_day_expiry(template):
    memories = template.find_resources("AWS::BedrockAgentCore::Memory")
    props = next(iter(memories.values()))["Properties"]
    assert props["EventExpiryDuration"] == 30
    strategies = props["MemoryStrategies"]
    assert len(strategies) == 4
    namespaces = []
    for strategy in strategies:
        (body,) = strategy.values()
        namespaces.extend(body["NamespaceTemplates"])
    assert sorted(namespaces) == sorted(
        [
            config.FACTS_NAMESPACE,
            config.PREFERENCES_NAMESPACE,
            config.SUMMARIES_NAMESPACE,
            config.EPISODES_NAMESPACE,
        ]
    )
    episodic = next(s["EpisodicMemoryStrategy"] for s in strategies if "EpisodicMemoryStrategy" in s)
    assert episodic["ReflectionConfiguration"]["NamespaceTemplates"] == [config.EPISODES_NAMESPACE]


def test_memory_options_can_be_changed(stub_zip):
    template = synth(
        stub_zip, memory_expiry_days="90", enable_episodic_memory="false", enable_summary_memory="false"
    )
    props = next(iter(template.find_resources("AWS::BedrockAgentCore::Memory").values()))["Properties"]
    assert props["EventExpiryDuration"] == 90
    assert len(props["MemoryStrategies"]) == 2


def test_invalid_expiry_is_rejected(stub_zip):
    with pytest.raises(ValueError):
        synth(stub_zip, memory_expiry_days="1")


def test_no_wildcard_resources_except_documented_cases(template):
    allowed_wildcards = {"XRayWrite", "RuntimeMetrics"}
    for statement in policy_statements(template):
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        if "*" in resources:
            assert statement.get("Sid") in allowed_wildcards, statement


def test_no_broad_managed_policies(template):
    for role in template.find_resources("AWS::IAM::Role").values():
        trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        if trust["Principal"].get("Service") == "bedrock-agentcore.amazonaws.com":
            assert not role["Properties"].get("ManagedPolicyArns")


def test_roles_have_confused_deputy_conditions(template):
    for role in template.find_resources("AWS::IAM::Role").values():
        trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        if trust["Principal"].get("Service") == "bedrock-agentcore.amazonaws.com":
            assert "aws:SourceAccount" in trust["Condition"]["StringEquals"]
            assert "aws:SourceArn" in trust["Condition"]["ArnLike"]


def test_bedrock_invoke_is_scoped_to_model_and_profile(template):
    statements = [s for s in policy_statements(template) if s.get("Sid") == "BedrockModelInvocation"]
    assert len(statements) == 1
    resources = statements[0]["Resource"]
    assert any("foundation-model/anthropic.claude-sonnet-4-6" in str(r) for r in resources)
    assert any("inference-profile/global.anthropic.claude-sonnet-4-6" in str(r) for r in resources)
    assert statements[0]["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]


def test_memory_data_plane_scoped_to_memory_arn(template):
    statements = [s for s in policy_statements(template) if s.get("Sid") == "MemoryDataPlane"]
    assert len(statements) == 2  # runtime role and memory role
    for statement in statements:
        assert "Fn::GetAtt" in str(statement["Resource"]) and "MemoryArn" in str(statement["Resource"])


def test_guardrail_context_adds_scoped_permission_and_env(stub_zip):
    template = synth(stub_zip, guardrail_id="abc123", guardrail_version="3")
    statements = [s for s in policy_statements(template) if s.get("Sid") == "ApplyGuardrail"]
    assert len(statements) == 1 and "guardrail/abc123" in str(statements[0]["Resource"])
    env = next(iter(template.find_resources("AWS::BedrockAgentCore::Runtime").values()))["Properties"][
        "EnvironmentVariables"
    ]
    assert env["BEDROCK_GUARDRAIL_ID"] == "abc123" and env["BEDROCK_GUARDRAIL_VERSION"] == "3"


def test_model_override_changes_iam_scope(stub_zip):
    template = synth(stub_zip, model_id="eu.anthropic.claude-sonnet-5-5")
    statement = next(s for s in policy_statements(template) if s.get("Sid") == "BedrockModelInvocation")
    assert any("anthropic.claude-sonnet-5-5" in str(r) for r in statement["Resource"])


def test_secret_has_no_literal_placeholder(template):
    assert "CHANGE_ME" not in str(template.to_json())
    template.resource_count_is("AWS::SecretsManager::Secret", 1)


def test_ssm_parameters_and_outputs_are_stable_contract(template):
    names = {
        r["Properties"]["Name"] for r in template.find_resources("AWS::SSM::Parameter").values()
    }
    assert names == {
        "/agentcore/agent_runtime_arn",
        "/agentcore/s3_bucket_name",
        "/agentcore/memory_id",
        "/agentcore/runtime_role_arn",
    }
    outputs = set(template.to_json()["Outputs"])
    assert {
        "AgentRuntimeArn",
        "AgentRuntimeId",
        "EndpointArn",
        "MemoryId",
        "S3BucketName",
        "TwelveLabsSecretArn",
    } <= outputs


def test_bucket_is_private_and_ssl_only(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {"PublicAccessBlockConfiguration": assertions.Match.object_like({"BlockPublicAcls": True})},
    )


def test_media_lifecycle_rules(template):
    bucket = next(iter(template.find_resources("AWS::S3::Bucket").values()))["Properties"]
    rules = bucket["LifecycleConfiguration"]["Rules"]
    assert {r["Prefix"] for r in rules} == {"images/", "voice/", "audio/", "video/", "documents/"}
    assert all(r["ExpirationInDays"] == 30 and r["Status"] == "Enabled" for r in rules)
