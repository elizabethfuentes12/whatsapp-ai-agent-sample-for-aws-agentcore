"""IAM role for AgentCore Runtime execution (least privilege)."""

from typing import Optional

from constructs import Construct
from aws_cdk import Aws, aws_iam as iam

from agentcore.config import RUNTIME_NAME, model_resource_arns


class AgentCoreRole(Construct):
    """Creates an IAM execution role for the AgentCore Runtime.

    Statements follow the documented direct-deploy execution role
    (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
    narrowed to this application: the memory ARN, the configured model and
    inference profile, the runtime log groups and the SSM parameters under
    ``/agentcore/``. Only X-Ray write APIs keep ``Resource: "*"`` because they
    do not support resource-level permissions.

    Args:
        memory_arn: ARN of the AgentCore Memory the agent reads and writes.
        model_id: Bedrock model or inference profile ID the agent invokes.
        guardrail_arn: Optional Bedrock Guardrail ARN the agent applies.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        memory_arn: str,
        model_id: str,
        guardrail_arn: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        partition, region, account = Aws.PARTITION, Aws.REGION, Aws.ACCOUNT_ID

        self.role = iam.Role(
            self,
            "AgentCoreExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:{partition}:bedrock-agentcore:{region}:{account}:*"},
                },
            ),
        )

        runtime_log_groups = f"arn:{partition}:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"

        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeLogGroups",
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                resources=[runtime_log_groups],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="UnifiedSpanDestination",
                actions=["logs:PutResourcePolicy"],
                resources=[
                    f"arn:{partition}:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{RUNTIME_NAME}*"
                ],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="DescribeLogGroups",
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:{partition}:logs:{region}:{account}:log-group:*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeLogStreams",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{runtime_log_groups}:log-stream:*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="XRayWrite",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="RuntimeMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockModelInvocation",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=model_resource_arns(model_id, region, account, partition),
            )
        )
        if guardrail_arn:
            self.role.add_to_policy(
                iam.PolicyStatement(
                    sid="ApplyGuardrail",
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[guardrail_arn],
                )
            )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryDataPlane",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                ],
                resources=[memory_arn],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="SharedParameters",
                actions=["ssm:GetParameter"],
                resources=[f"arn:{partition}:ssm:{region}:{account}:parameter/agentcore/*"],
            )
        )
