"""API Gateway webhook construct for the WhatsApp and Instagram Cloud APIs.

The webhook is public on purpose: the caller is Meta. Authentication is the
`X-Hub-Signature-256` HMAC check inside the Lambda (fails closed). API Gateway adds
the layers that do not need the secret: parameter validation (400 before the Lambda is
invoked), stage throttling, access logs, X-Ray tracing and an optional WAF rate rule.
There is no CORS configuration because Meta calls the endpoint server to server.
"""

from constructs import Construct
from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_apigateway as apg,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_wafv2 as wafv2,
)

from whatsapp_api_gateway.config import DEFAULT_THROTTLE_BURST, DEFAULT_THROTTLE_RATE

STAGE_NAME = "prod"


class WebhookApi(Construct):
    """REST API Gateway that receives WhatsApp and Instagram webhook events."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        whatsapp_in_fn: _lambda.Function,
        throttle_rate: int = DEFAULT_THROTTLE_RATE,
        throttle_burst: int = DEFAULT_THROTTLE_BURST,
        enable_waf: bool = False,
        waf_rate_limit: int = 2000,
        log_retention: logs.RetentionDays = logs.RetentionDays.ONE_MONTH,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.access_logs = logs.LogGroup(
            self,
            "AccessLogs",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.api = apg.RestApi(
            self,
            "WhatsAppWebhookApi",
            rest_api_name="WhatsApp Webhook API",
            description="Receives WhatsApp and Instagram webhook events from Meta",
            # Access logs need the account level CloudWatch role (shared by all APIs in the Region).
            cloud_watch_role=True,
            deploy_options=apg.StageOptions(
                stage_name=STAGE_NAME,
                throttling_rate_limit=throttle_rate,
                throttling_burst_limit=throttle_burst,
                tracing_enabled=True,
                metrics_enabled=True,
                access_log_destination=apg.LogGroupLogDestination(self.access_logs),
                # Request id, source IP, method, path, status and size only: no headers or bodies.
                access_log_format=apg.AccessLogFormat.json_with_standard_fields(
                    caller=False,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=False,
                ),
            ),
        )

        validator = self.api.add_request_validator(
            "ParameterValidator", validate_request_parameters=True, validate_request_body=False
        )

        webhook_resource = self.api.root.add_resource("webhook")
        integration = apg.LambdaIntegration(whatsapp_in_fn, allow_test_invoke=False)

        # Verification (GET): Meta always sends these three query parameters.
        webhook_resource.add_method(
            "GET",
            integration,
            request_validator=validator,
            request_parameters={
                "method.request.querystring.hub.mode": True,
                "method.request.querystring.hub.challenge": True,
                "method.request.querystring.hub.verify_token": True,
            },
        )
        # Event notifications (POST): the signature header is mandatory. API Gateway rejects a
        # request without it with 400; the Lambda still verifies the signature itself.
        webhook_resource.add_method(
            "POST",
            integration,
            request_validator=validator,
            request_parameters={"method.request.header.X-Hub-Signature-256": True},
        )

        self.web_acl = None
        if enable_waf:
            self._add_waf(waf_rate_limit)

        CfnOutput(
            scope,
            "WebhookUrl",
            value=f"{self.api.url}webhook",
            description="Webhook URL to configure in the Meta Developer Portal (WhatsApp and Instagram)",
        )

    def _add_waf(self, rate_limit: int) -> None:
        """Regional web ACL with a per-IP rate rule and AWS managed common rules."""
        stack = Stack.of(self)
        self.web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="WebhookWebAcl",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIp",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=rate_limit, aggregate_key_type="IP"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="RateLimitPerIp",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )
        association = wafv2.CfnWebACLAssociation(
            self,
            "WebAclAssociation",
            resource_arn=(
                f"arn:{stack.partition}:apigateway:{stack.region}::"
                f"/restapis/{self.api.rest_api_id}/stages/{STAGE_NAME}"
            ),
            web_acl_arn=self.web_acl.attr_arn,
        )
        association.node.add_dependency(self.api.deployment_stage)
