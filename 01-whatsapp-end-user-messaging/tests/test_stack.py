"""CDK assertions for stack 01 (run offline: SSM values are CloudFormation parameters)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from whatsapp_end_user_messaging.whatsapp_stack import WhatsAppEndUserMessagingStack


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = WhatsAppEndUserMessagingStack(
        app, "Test", env=cdk.Environment(account="123456789012", region="us-east-1")
    )
    return Template.from_stack(stack)


def _functions(template):
    return template.find_resources("AWS::Lambda::Function")


def test_ssm_parameters_resolved_at_deploy_time(template):
    params = template.to_json()["Parameters"]
    types = {v["Type"] for k, v in params.items() if "SsmParameter" in k}
    assert types == {"AWS::SSM::Parameter::Value<String>"}
    names = {v["Default"] for v in params.values() if str(v.get("Type", "")).startswith("AWS::SSM")}
    assert {"/agentcore/agent_runtime_arn", "/agentcore/runtime_role_arn"} <= names


def test_lambdas_use_python313_arm64_and_tracing(template):
    functions = [
        f for f in _functions(template).values()
        if "TABLE_NAME" in f["Properties"].get("Environment", {}).get("Variables", {})
    ]
    assert len(functions) == 2
    for fn in functions:
        props = fn["Properties"]
        assert props["Runtime"] == "python3.13"
        assert props["Architectures"] == ["arm64"]
        assert props["TracingConfig"] == {"Mode": "Active"}
        assert "LoggingConfig" in props


def test_log_groups_have_retention(template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == 2
    assert all(g["Properties"]["RetentionInDays"] == 30 for g in groups.values())


def test_handler_has_dead_letter_queue_and_retry_limits(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": Match.object_like({"EXPECTED_TOPIC_ARN": Match.any_value()})},
         "DeadLetterConfig": {"TargetArn": Match.any_value()}},
    )
    template.has_resource_properties(
        "AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 2}
    )


def test_stream_source_has_dlq_bisect_and_partial_batch(template):
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "BisectBatchOnFunctionError": True,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "MaximumRetryAttempts": 3,
            "MaximumRecordAgeInSeconds": 3600,
            "TumblingWindowInSeconds": 20,
            "DestinationConfig": {"OnFailure": {"Destination": Match.any_value()}},
        },
    )


def test_topic_policy_is_scoped_to_social_messaging_in_this_account(template):
    policies = template.find_resources("AWS::SNS::TopicPolicy")
    statements = [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]]
    publish = [s for s in statements if s.get("Sid") == "AllowEndUserMessagingSocialPublish"]
    assert len(publish) == 1
    assert publish[0]["Principal"] == {"Service": "social-messaging.amazonaws.com"}
    assert "aws:SourceAccount" in json.dumps(publish[0]["Condition"])


def _all_statements(template):
    out = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def _actions(statement):
    action = statement["Action"]
    return [action] if isinstance(action, str) else action


def test_no_wildcard_resources_on_sensitive_actions(template):
    sensitive = ("bedrock-agentcore:", "social-messaging:", "transcribe:", "dynamodb:PutItem", "dynamodb:UpdateItem")
    for statement in _all_statements(template):
        if statement["Effect"] != "Allow":
            continue
        if any(a.startswith(sensitive) for a in _actions(statement)):
            assert statement["Resource"] != "*", statement


def test_invoke_is_scoped_to_the_runtime_arn(template):
    invoke = [s for s in _all_statements(template)
              if "bedrock-agentcore:InvokeAgentRuntime" in _actions(s)]
    assert len(invoke) == 1
    assert "bedrock-agentcore:InvokeAgentRuntimeForUser" in invoke[0]["Action"]
    assert len(invoke[0]["Resource"]) == 2
    assert "runtime-endpoint" in json.dumps(invoke[0]["Resource"][1])


def test_social_messaging_is_scoped_to_phone_numbers(template):
    for statement in _all_statements(template):
        if any(a.startswith("social-messaging:") for a in _actions(statement)):
            assert "phone-number-id/*" in json.dumps(statement["Resource"])


def test_table_has_ttl_stream_and_sse(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            "StreamSpecification": {"StreamViewType": "NEW_IMAGE"},
            "SSESpecification": {"SSEEnabled": True},
        },
    )


def test_bucket_is_private_encrypted_and_expires_media(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True, "BlockPublicPolicy": True,
                "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.any_value(),
            "LifecycleConfiguration": {"Rules": Match.array_with([
                Match.object_like({"Prefix": "images/", "ExpirationInDays": 30}),
            ])},
        },
    )


def test_graph_api_version_is_v25_and_overridable(template):
    for fn in _functions(template).values():
        variables = fn["Properties"].get("Environment", {}).get("Variables", {})
        if "META_API_VERSION" in variables:
            assert variables["META_API_VERSION"] == "v25.0"

    app = cdk.App(context={"meta_api_version": "v26.0"})
    stack = WhatsAppEndUserMessagingStack(app, "Other")
    other = Template.from_stack(stack)
    versions = {
        f["Properties"]["Environment"]["Variables"]["META_API_VERSION"]
        for f in other.find_resources("AWS::Lambda::Function").values()
        if "META_API_VERSION" in f["Properties"].get("Environment", {}).get("Variables", {})
    }
    assert versions == {"v26.0"}


def test_retain_data_switches_policies():
    app = cdk.App(context={"retain_data": "true"})
    stack = WhatsAppEndUserMessagingStack(app, "Keep")
    keep = Template.from_stack(stack)
    keep.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain",
                                               "Properties": {"DeletionProtectionEnabled": True}})
