"""API Gateway webhook construct for WhatsApp Cloud API."""

from constructs import Construct
from aws_cdk import (
    CfnOutput,
    aws_apigateway as apg,
    aws_lambda as _lambda,
)


class WebhookApi(Construct):
    """REST API Gateway that receives WhatsApp webhook events."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        whatsapp_in_fn: _lambda.Function,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.api = apg.RestApi(
            self,
            "WhatsAppWebhookApi",
            rest_api_name="WhatsApp Webhook API",
            description="Receives WhatsApp Cloud API webhook events",
        )

        self.api.root.add_cors_preflight(
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

        webhook_resource = self.api.root.add_resource(
            "webhook",
            default_integration=apg.LambdaIntegration(
                whatsapp_in_fn, allow_test_invoke=False
            ),
        )

        webhook_resource.add_method("GET")   # Verification
        webhook_resource.add_method("POST")  # Message reception

        CfnOutput(
            scope,
            "WebhookUrl",
            value=f"{self.api.url}webhook",
            description="WhatsApp webhook URL to configure in Meta Developer Portal",
        )
