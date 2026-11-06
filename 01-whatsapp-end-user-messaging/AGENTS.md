# AGENTS.md: 01-whatsapp-end-user-messaging

## What this app does

CDK app (Python) that connects WhatsApp to the AgentCore agent from `00-agent-agentcore` through AWS End User Messaging Social. Messages arrive on an SNS topic, a receiver Lambda stores them in DynamoDB, a DynamoDB Stream tumbling window buffers them, and a processor Lambda aggregates each sender's messages, calls the AgentCore Runtime once and replies on WhatsApp.

## Structure

| Path | Purpose |
|---|---|
| `app.py`, `cdk.json` | CDK entry point (stack `WhatsAppEndUserMessagingStack`) |
| `whatsapp_end_user_messaging/whatsapp_stack.py` | Stack: SSM parameters, media bucket (lifecycle), table, topic, Lambdas, outputs |
| `whatsapp_end_user_messaging/config.py` | Graph API version constant (`v25.0`), TTL, media limit, context helpers |
| `topic/topic.py` | SNS topic; publish allowed only for `social-messaging.amazonaws.com` in this account |
| `databases/databases.py` | Messages table (stream, TTL 48 h, SSE); the item is also the idempotency record |
| `lambdas/project_lambdas.py` | Both Lambdas (python3.13, arm64, tracing, log groups), DLQs, event source mapping, scoped IAM |
| `lambdas/code/whatsapp_handler/` | Receiver: SNS record check, media to S3, conditional put (`lambda_function.py`, `whatsapp_service.py`, `safe_logging.py`) |
| `lambdas/code/message_processor/` | Processor: claim, aggregate, transcribe, invoke, reply (`lambda_function.py`, `agentcore_service.py`, `idempotency.py`, `message_utils.py`, `safe_logging.py`) |
| `tests/` | pytest: pure helpers, idempotency, receiver, processor, CDK assertions |

## Commands

```bash
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall lambdas tests whatsapp_end_user_messaging
export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1
npx --yes aws-cdk@latest synth --quiet      # works offline: SSM values are CloudFormation parameters
```

Deploy (stack 00 must be deployed first): `npx aws-cdk@latest deploy`.
Then link the WhatsApp Business Account in the End User Messaging Social console and choose the `WhatsAppTopicArn` output as the event destination.

CDK context (`-c key=value`): `meta_api_version` (default `v25.0`), `media_retention_days` (30), `retain_data` (true switches table and bucket to RETAIN and turns on deletion protection and PITR), `processor_reserved_concurrency` (unset by default), `topic_source_arn_pattern` (adds an `aws:SourceArn` condition to the topic policy).

## Dependencies and environment

`aws-cdk-lib==2.275.0`; Lambdas use the Python 3.13 runtime's boto3. SSM parameters read at deploy time: `/agentcore/agent_runtime_arn`, `/agentcore/runtime_role_arn` (written by stack 00). Lambda environment: `TABLE_NAME`, `S3_BUCKET`, `META_API_VERSION`, `EXPECTED_TOPIC_ARN` (receiver), `AGENT_ARN`, `MAX_INLINE_MEDIA_BYTES` (processor). Needs Bedrock AgentCore, End User Messaging Social, Transcribe and a CDK bootstrapped account. No secrets are stored by this stack.

## Conventions and gotchas

* Authentication: SNS to Lambda is IAM authenticated (topic policy + Lambda resource policy); the receiver also rejects any record whose `EventSource` is not `aws:sns` or whose `TopicArn` differs from `EXPECTED_TOPIC_ARN` (fails closed when unset). There is no Meta HMAC here and no public endpoint.
* Idempotency: the receiver uses a conditional put on `(from_phone, id)`. The processor claims each message (`proc_state` processing then done) before work and releases it on failure. Keep both when changing the flow; the tumbling window may deliver a record more than once.
* The processor response must always contain `state` (tumbling window rule). Failed senders are returned in `batchItemFailures` with the stream `SequenceNumber`.
* The Lambda payload to the agent includes `idempotency_key` (the last message id, hashed above 128 characters), `actor_id` and the session id. Actor and session ids are padded to 33 characters with `ljust(33, "0")`. The `[Channel: whatsapp]` tag must stay first in the prompt (the agent reads it).
* Logging: JSON lines with message ids, counts and exception types only. Never log message text, phone numbers (use `mask_phone`), headers or provider error bodies.
* IAM: `social-messaging:*` is limited to `phone-number-id/*`, invoke permissions to the runtime ARN and `/runtime-endpoint/*`, Transcribe to `transcription-job/whatsapp-audio-*`. `tests/test_stack.py` fails on `Resource: "*"` for these actions.
* Processor time budget is 10 minutes: transcription at most 2 minutes, AgentCore calls stop when less than 30 seconds are left.
* `GetWhatsAppMessageMedia` writes under the configured prefix with the caller's credentials; the receiver therefore has `s3:PutObject` on the bucket.
* Do not reintroduce synth-time AWS calls (the old `get_param.py` used boto3 at synth).

## Current state

Modernized in FEAT-003 (see `docs/AUDIT.md` section 9): Python 3.13 arm64 Lambdas, Graph API `v25.0`, deploy-time SSM resolution, scoped IAM, SNS source checks and topic policy conditions, durable dedupe and claims, stream failure handling (bisect, partial batch response, SQS destination), handler DLQ, tracing, log retention, media lifecycle, SSE and PITR options, friendly user errors, size limits, and 41 offline tests.
