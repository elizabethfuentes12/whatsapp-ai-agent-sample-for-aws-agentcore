"""AgentCore Memory construct using CfnMemory."""

from constructs import Construct
from aws_cdk import (
    aws_bedrockagentcore as bedrockagentcore,
    aws_iam as iam,
)

MEMORY_EXPIRY_DAYS = 3


class AgentCoreMemory(Construct):
    """Creates an AgentCore Memory with semantic and user preference strategies."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        memory_role = iam.Role(
            self,
            "MemoryExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        memory_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:DeleteMemoryRecord",
                ],
                resources=["*"],
            )
        )

        self.memory = bedrockagentcore.CfnMemory(
            self,
            "Memory",
            name="WhatsAppAgentMemoryV2",
            description="Long-term memory for WhatsApp agent — stores user facts and preferences across sessions",
            event_expiry_duration=MEMORY_EXPIRY_DAYS,
            memory_execution_role_arn=memory_role.role_arn,
            memory_strategies=[
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=bedrockagentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="UserFacts",
                        description="Extracts and stores factual information from conversations — names, events, topics discussed, multimedia descriptions",
                    )
                ),
                bedrockagentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=bedrockagentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name="UserPreferences",
                        description="Tracks user preferences — language, communication style, interests, and recurring topics",
                    )
                ),
            ],
        )

    @property
    def memory_id(self) -> str:
        return self.memory.attr_memory_id

    @property
    def memory_arn(self) -> str:
        return self.memory.attr_memory_arn
