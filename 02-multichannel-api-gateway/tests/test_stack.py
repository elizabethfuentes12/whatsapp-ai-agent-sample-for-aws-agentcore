"""CDK assertions for stack 02 (offline: SSM values are CloudFormation parameters)."""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from whatsapp_api_gateway.whatsapp_stack import WhatsAppApiGatewayStack

LAYER_SOURCES = str(Path(__file__).resolve().parents[1] / "layers" / "common")  # skips the dependency build
ENV = cdk.Environment(account="123456789012", region="us-east-1")


def build(context=None):
    app = cdk.App(context={"layer_path": LAYER_SOURCES, **(context or {})})
    return Template.from_stack(WhatsAppApiGatewayStack(app, "Test", env=ENV))


@pytest.fixture(scope="module")
def template():
    return build()


def _app_functions(template):
    return [
        f for f in template.find_resources("AWS::Lambda::Function").values()
        if "TABLE_NAME" in f["Properties"].get("Environment", {}).get("Variables", {})
    ]


def _statements(template):
    out = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def _actions(statement):
    action = statement["Action"]
    return [action] if isinstance(action, str) else action


def test_ssm_values_are_deploy_time_parameters(template):
    params = template.to_json()["Parameters"]
    names = {v["Default"] for v in params.values() if str(v.get("Type", "")).startswith("AWS::SSM")}
    assert {"/agentcore/agent_runtime_arn", "/agentcore/runtime_role_arn"} <= names


def test_unified_users_table_name_is_still_published(template):
    template.has_resource_properties(
        "AWS::SSM::Parameter", {"Name": "/agentcore/unified_users_table_name", "Type": "String"}
    )


def test_lambdas_use_python313_arm64_tracing_and_layer(template):
    functions = _app_functions(template)
    assert len(functions) == 2
    for fn in functions:
        props = fn["Properties"]
        assert props["Runtime"] == "python3.13"
        assert props["Architectures"] == ["arm64"]
        assert props["TracingConfig"] == {"Mode": "Active"}
        assert props["Environment"]["Variables"]["GRAPH_API_VERSION"] == "v25.0"
        assert len(props["Layers"]) == 1
    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {"CompatibleRuntimes": ["python3.13"], "CompatibleArchitectures": ["arm64"]},
    )


def test_receiver_timeout_fits_api_gateway_limit(template):
    receiver = [f for f in _app_functions(template) if "SECRET_ARN" in f["Properties"]["Environment"]["Variables"]
                and "AGENT_ARN" not in f["Properties"]["Environment"]["Variables"]]
    assert receiver and receiver[0]["Properties"]["Timeout"] < 29


def test_log_groups_have_retention(template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == 3  # two Lambdas + API access logs
    assert all(g["Properties"]["RetentionInDays"] == 30 for g in groups.values())


def test_no_cors_anywhere(template):
    resources = template.find_resources("AWS::ApiGateway::Method")
    assert {r["Properties"]["HttpMethod"] for r in resources.values()} == {"GET", "POST"}  # no OPTIONS
    assert "Access-Control" not in json.dumps(template.to_json())


def test_only_the_webhook_resource_is_exposed(template):
    paths = {r["Properties"]["PathPart"] for r in template.find_resources("AWS::ApiGateway::Resource").values()}
    assert paths == {"webhook"}
    for method in template.find_resources("AWS::ApiGateway::Method").values():
        assert method["Properties"]["AuthorizationType"] == "NONE"  # auth is the HMAC signature in the Lambda


def test_post_requires_the_signature_header_and_get_requires_hub_params(template):
    methods = {m["Properties"]["HttpMethod"]: m["Properties"] for m in
               template.find_resources("AWS::ApiGateway::Method").values()}
    assert methods["POST"]["RequestParameters"] == {"method.request.header.X-Hub-Signature-256": True}
    assert set(methods["GET"]["RequestParameters"]) == {
        "method.request.querystring.hub.mode",
        "method.request.querystring.hub.challenge",
        "method.request.querystring.hub.verify_token",
    }
    template.has_resource_properties("AWS::ApiGateway::RequestValidator", {"ValidateRequestParameters": True})


def test_stage_has_throttling_tracing_and_access_logs(template):
    template.has_resource_properties(
        "AWS::ApiGateway::Stage",
        {
            "TracingEnabled": True,
            "AccessLogSetting": {"DestinationArn": Match.any_value(), "Format": Match.any_value()},
            "MethodSettings": Match.array_with([
                Match.object_like({"ThrottlingRateLimit": 50, "ThrottlingBurstLimit": 100, "HttpMethod": "*"}),
            ]),
        },
    )


def test_access_log_format_has_no_headers_or_bodies(template):
    stages = template.find_resources("AWS::ApiGateway::Stage")
    fmt = next(iter(stages.values()))["Properties"]["AccessLogSetting"]["Format"]
    assert "$context.requestId" in fmt
    assert "Authorization" not in fmt and "requestBody" not in fmt and "$input" not in fmt


def test_throttling_is_configurable():
    other = build({"throttle_rate_limit": "5", "throttle_burst_limit": "10"})
    other.has_resource_properties(
        "AWS::ApiGateway::Stage",
        {"MethodSettings": Match.array_with([Match.object_like({"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10})])},
    )


def test_waf_is_optional(template):
    template.resource_count_is("AWS::WAFv2::WebACL", 0)
    on = build({"enable_waf": "true"})
    on.resource_count_is("AWS::WAFv2::WebACL", 1)
    on.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)
    on.has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "REGIONAL"})


def test_stream_source_has_dlq_bisect_and_partial_batch(template):
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "BisectBatchOnFunctionError": True,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "MaximumRetryAttempts": 3,
            "MaximumRecordAgeInSeconds": 3600,
            "TumblingWindowInSeconds": 10,
            "DestinationConfig": {"OnFailure": {"Destination": Match.any_value()}},
        },
    )
    template.resource_count_is("AWS::SQS::Queue", 1)


def test_invoke_is_scoped_to_the_runtime_arn(template):
    invoke = [s for s in _statements(template) if "bedrock-agentcore:InvokeAgentRuntime" in _actions(s)]
    assert len(invoke) == 1
    assert "bedrock-agentcore:InvokeAgentRuntimeForUser" in invoke[0]["Action"]
    assert len(invoke[0]["Resource"]) == 2
    assert "runtime-endpoint" in json.dumps(invoke[0]["Resource"][1])


def test_no_wildcard_resources_on_sensitive_actions(template):
    sensitive = ("bedrock-agentcore:", "transcribe:", "secretsmanager:", "dynamodb:PutItem", "dynamodb:UpdateItem")
    for statement in _statements(template):
        if statement["Effect"] == "Allow" and any(a.startswith(sensitive) for a in _actions(statement)):
            assert statement["Resource"] != "*", statement


def test_receiver_cannot_read_or_invoke_the_agent(template):
    # The receiver role must not carry processor permissions.
    receiver_policy = [p for p in template.find_resources("AWS::IAM::Policy").values()
                       if "WebhookReceiver" in json.dumps(p["Properties"]["Roles"])]
    actions = {a for p in receiver_policy for s in p["Properties"]["PolicyDocument"]["Statement"] for a in _actions(s)}
    assert not any(a.startswith(("bedrock-agentcore:", "transcribe:")) for a in actions)
    assert "dynamodb:PutItem" in actions and "dynamodb:Scan" not in actions


def test_generated_app_secrets(template):
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == 2
    keys = {s["Properties"]["GenerateSecretString"]["GenerateStringKey"] for s in secrets.values()}
    assert keys == {"WHATS_APP_SECRET", "IG_APP_SECRET"}  # random until replaced by the Meta app secrets


def test_tables_have_ttl_sse_and_users_pitr(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
         "StreamSpecification": {"StreamViewType": "NEW_IMAGE"}, "SSESpecification": {"SSEEnabled": True}},
    )
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
         "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
         "SSESpecification": {"SSEEnabled": True}},
    )


def test_retain_data_context_retains_tables_and_bucket():
    keep = build({"retain_data": "true"})
    keep.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain",
                                               "Properties": {"DeletionProtectionEnabled": True}})
    keep.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Retain"})


def test_bucket_private_encrypted_with_media_expiry(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True, "BlockPublicPolicy": True,
                "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.any_value(),
            "LifecycleConfiguration": {"Rules": Match.array_with([
                Match.object_like({"Prefix": "audio/", "ExpirationInDays": 30}),
                Match.object_like({"Prefix": "ig-images/", "ExpirationInDays": 30}),
            ])},
        },
    )


def test_graph_api_version_override():
    other = build({"graph_api_version": "v26.0"})
    versions = {
        f["Properties"]["Environment"]["Variables"]["GRAPH_API_VERSION"]
        for f in _app_functions(other)
    }
    assert versions == {"v26.0"}
