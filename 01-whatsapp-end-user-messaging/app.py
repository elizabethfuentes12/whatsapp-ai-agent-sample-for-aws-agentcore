#!/usr/bin/env python3
"""CDK App entry point for WhatsApp End User Messaging integration."""

import aws_cdk as cdk

from whatsapp_end_user_messaging.whatsapp_stack import WhatsAppEndUserMessagingStack

app = cdk.App()
WhatsAppEndUserMessagingStack(app, "WhatsAppEndUserMessagingStack")
app.synth()
