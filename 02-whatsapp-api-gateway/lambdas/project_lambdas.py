"""Lambda function constructs for the API Gateway WhatsApp integration."""

from constructs import Construct
from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_dynamodb as ddb,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_lambda_event_sources as event_sources,
)


class ProjectLambdas(Construct):
    """All Lambda functions for the WhatsApp API Gateway pipeline."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        table: ddb.Table,
        bucket: s3.Bucket,
        common_layer: _lambda.LayerVersion,
        secret_arn: str,
        agent_runtime_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        default_env = {
            "TABLE_NAME": table.table_name,
            "S3_BUCKET": bucket.bucket_name,
        }

        # --- whatsapp_in: Webhook receiver ---
        self.whatsapp_in = _lambda.Function(
            self,
            "WhatsAppIn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/whatsapp_in"),
            timeout=Duration.seconds(30),
            memory_size=256,
            layers=[common_layer],
            environment={
                **default_env,
                "SECRET_ARN": secret_arn,
                "DISPLAY_PHONE_NUMBER": "",
            },
        )

        # --- whatsapp_out: Message sender ---
        self.whatsapp_out = _lambda.Function(
            self,
            "WhatsAppOut",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/whatsapp_out"),
            timeout=Duration.seconds(30),
            memory_size=256,
            layers=[common_layer],
            environment=default_env,
        )

        # --- agent_processor: AgentCore invoker ---
        self.agent_processor = _lambda.Function(
            self,
            "AgentProcessor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/agent_processor"),
            timeout=Duration.minutes(10),
            memory_size=512,
            layers=[common_layer],
            environment={
                **default_env,
                "AGENT_ARN": agent_runtime_arn,
                "WHATSAPP_OUT_LAMBDA": "",  # Set after creation
            },
        )

        # --- audio_transcriptor: Starts transcription jobs ---
        self.audio_transcriptor = _lambda.Function(
            self,
            "AudioTranscriptor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/audio_transcriptor"),
            timeout=Duration.minutes(5),
            memory_size=256,
            layers=[common_layer],
            environment={
                **default_env,
                "WHATSAPP_OUT_LAMBDA": "",  # Set after creation
            },
        )

        # --- transcriber_done: S3 event on transcription output ---
        self.transcriber_done = _lambda.Function(
            self,
            "TranscriberDone",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/transcriber_done"),
            timeout=Duration.minutes(2),
            memory_size=256,
            layers=[common_layer],
            environment={
                **default_env,
                "AGENT_PROCESSOR_LAMBDA": "",  # Set after creation
            },
        )

        # --- process_stream: DynamoDB stream processor ---
        self.process_stream = _lambda.Function(
            self,
            "ProcessStream",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/code/process_stream"),
            timeout=Duration.minutes(2),
            memory_size=256,
            layers=[common_layer],
            environment={
                **default_env,
                "AGENT_PROCESSOR_LAMBDA": "",  # Set after creation
                "WHATSAPP_OUT_LAMBDA": "",     # Set after creation
                "AUDIO_TRANSCRIPTOR_LAMBDA": "",  # Set after creation
            },
        )

        # --- Wire up Lambda function names ---
        self.whatsapp_out.function_name  # Force creation order

        self.agent_processor.add_environment(
            "WHATSAPP_OUT_LAMBDA", self.whatsapp_out.function_name
        )
        self.audio_transcriptor.add_environment(
            "WHATSAPP_OUT_LAMBDA", self.whatsapp_out.function_name
        )
        self.transcriber_done.add_environment(
            "AGENT_PROCESSOR_LAMBDA", self.agent_processor.function_name
        )
        self.process_stream.add_environment(
            "AGENT_PROCESSOR_LAMBDA", self.agent_processor.function_name
        )
        self.process_stream.add_environment(
            "WHATSAPP_OUT_LAMBDA", self.whatsapp_out.function_name
        )
        self.process_stream.add_environment(
            "AUDIO_TRANSCRIPTOR_LAMBDA", self.audio_transcriptor.function_name
        )

        # --- Permissions ---

        # DynamoDB
        table.grant_read_write_data(self.whatsapp_in)
        table.grant_read_data(self.transcriber_done)
        table.grant_read_write_data(self.audio_transcriptor)

        # DynamoDB Stream
        self.process_stream.add_event_source(
            event_sources.DynamoEventSource(
                table,
                starting_position=_lambda.StartingPosition.LATEST,
                batch_size=1,
            )
        )

        # S3
        bucket.grant_read_write(self.agent_processor)
        bucket.grant_read_write(self.audio_transcriptor)
        bucket.grant_read(self.transcriber_done)

        # S3 notification for transcription output
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.transcriber_done),
            s3.NotificationKeyFilter(prefix="transcriptions/"),
        )

        # Lambda invoke permissions (cross-Lambda calls)
        self.whatsapp_out.grant_invoke(self.process_stream)
        self.whatsapp_out.grant_invoke(self.agent_processor)
        self.whatsapp_out.grant_invoke(self.audio_transcriptor)
        self.agent_processor.grant_invoke(self.process_stream)
        self.agent_processor.grant_invoke(self.transcriber_done)
        self.audio_transcriptor.grant_invoke(self.process_stream)

        # Secrets Manager
        self.whatsapp_in.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[secret_arn],
            )
        )

        # AgentCore Runtime
        self.agent_processor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=["*"],
            )
        )

        # Transcribe
        self.audio_transcriptor.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                ],
                resources=["*"],
            )
        )
