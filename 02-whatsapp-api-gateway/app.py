#!/usr/bin/env python3
"""CDK App entry point for WhatsApp API Gateway integration."""

import aws_cdk as cdk

from whatsapp_api_gateway.whatsapp_stack import WhatsAppApiGatewayStack

app = cdk.App()
WhatsAppApiGatewayStack(app, "WhatsAppApiGatewayStack")
app.synth()
