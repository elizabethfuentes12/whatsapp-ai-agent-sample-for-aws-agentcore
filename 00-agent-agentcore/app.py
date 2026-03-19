#!/usr/bin/env python3
"""CDK App entry point for standalone AgentCore Runtime deployment."""

import aws_cdk as cdk

from agent_agentcore.agent_agentcore_stack import AgentAgentCoreStack

app = cdk.App()
AgentAgentCoreStack(app, "AgentAgentCoreStack")
app.synth()
