"""AgentCore Memory construct using CfnMemory."""

from typing import Optional

from constructs import Construct
from aws_cdk import (
    Aws,
    aws_bedrockagentcore as bedrockagentcore,
    aws_iam as iam,
)

from agentcore.config import (
    DEFAULT_MEMORY_EXPIRY_DAYS,
    DEFAULT_MODEL_ID,
    EPISODES_NAMESPACE,
    FACTS_NAMESPACE,
    PREFERENCES_NAMESPACE,
    SUMMARIES_NAMESPACE,
    model_resource_arns,
)

_MemoryStrategy = bedrockagentcore.CfnMemory.MemoryStrategyProperty


class AgentCoreMemory(Construct):
    """Creates an AgentCore Memory with semantic, preference, summary and episodic strategies.

    Every strategy declares an explicit namespace template, so the agent can
    retrieve from a known path without knowing the generated strategy IDs.

    Args:
        expiry_days: Retention of short-term events (days). A longer window lets
            a user return after a few days and still see recent turns, at the
            cost of more stored data. Long-term records are stored separately.
        enable_summary: Add the session summary strategy.
        enable_episodic: Add the episodic strategy (episodes plus reflections).
        model_id: Model used for long-term extraction, only used to scope IAM.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        expiry_days: int = DEFAULT_MEMORY_EXPIRY_DAYS,
        enable_summary: bool = True,
        enable_episodic: bool = True,
        model_id: str = DEFAULT_MODEL_ID,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        memory_role = iam.Role(
            self,
            "MemoryExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:bedrock-agentcore:{Aws.REGION}:{Aws.ACCOUNT_ID}:*"
                    },
                },
            ),
        )

        strategies = [
            _MemoryStrategy(
                semantic_memory_strategy=bedrockagentcore.CfnMemory.SemanticMemoryStrategyProperty(
                    name="UserFacts",
                    description="Extracts and stores factual information from conversations: names, events, topics discussed, multimedia descriptions",
                    namespace_templates=[FACTS_NAMESPACE],
                )
            ),
            _MemoryStrategy(
                user_preference_memory_strategy=bedrockagentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                    name="UserPreferences",
                    description="Tracks user preferences: language, communication style, interests, and recurring topics",
                    namespace_templates=[PREFERENCES_NAMESPACE],
                )
            ),
        ]
        if enable_summary:
            strategies.append(
                _MemoryStrategy(
                    summary_memory_strategy=bedrockagentcore.CfnMemory.SummaryMemoryStrategyProperty(
                        name="SessionSummaries",
                        description="Condensed summaries of each conversation session",
                        namespace_templates=[SUMMARIES_NAMESPACE],
                    )
                )
            )
        if enable_episodic:
            # Reflections are written at actor level (same namespace as the
            # episodes), so insights never mix data from different users.
            strategies.append(
                _MemoryStrategy(
                    episodic_memory_strategy=bedrockagentcore.CfnMemory.EpisodicMemoryStrategyProperty(
                        name="Episodes",
                        description="Completed interaction episodes and reflections across episodes",
                        namespace_templates=[EPISODES_NAMESPACE],
                        reflection_configuration=bedrockagentcore.CfnMemory.EpisodicReflectionConfigurationInputProperty(
                            namespace_templates=[EPISODES_NAMESPACE],
                        ),
                    )
                )
            )

        self.memory = bedrockagentcore.CfnMemory(
            self,
            "Memory",
            name="WhatsAppAgentMemoryV2",
            description="Memory for the WhatsApp agent: facts, preferences, summaries and episodes across sessions",
            event_expiry_duration=expiry_days,
            memory_execution_role_arn=memory_role.role_arn,
            memory_strategies=strategies,
        )

        # Attached as a separate policy (not inline) so it can reference the
        # memory ARN without creating a Role -> Memory -> Role cycle.
        iam.Policy(
            self,
            "MemoryRolePolicy",
            roles=[memory_role],
            statements=[
                iam.PolicyStatement(
                    sid="ExtractionModel",
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=model_resource_arns(model_id, Aws.REGION, Aws.ACCOUNT_ID, Aws.PARTITION),
                ),
                iam.PolicyStatement(
                    sid="MemoryDataPlane",
                    actions=[
                        "bedrock-agentcore:GetEvent",
                        "bedrock-agentcore:ListEvents",
                        "bedrock-agentcore:RetrieveMemoryRecords",
                        "bedrock-agentcore:ListMemoryRecords",
                    ],
                    resources=[self.memory.attr_memory_arn],
                ),
            ],
        )

    @property
    def memory_id(self) -> str:
        return self.memory.attr_memory_id

    @property
    def memory_arn(self) -> str:
        return self.memory.attr_memory_arn
