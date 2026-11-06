# AGENTS.md: 02-multichannel-api-gateway

## What this app does

CDK app (Python) that receives WhatsApp and Instagram DM webhooks from Meta through one API Gateway endpoint, buffers messages in DynamoDB, and replies through the AgentCore agent from `00-agent-agentcore`. It also owns the unified users table that links WhatsApp and Instagram identities so both channels share one AgentCore memory.

## Structure

| Path | Purpose |
|---|---|
| `app.py`, `cdk.json` | CDK entry point (stack `WhatsAppApiGatewayStack`) |
| `whatsapp_api_gateway/whatsapp_stack.py` | Stack: SSM parameters, secrets, bucket, tables, layer, Lambdas, API, outputs |
| `whatsapp_api_gateway/config.py` | Graph API version (`v25.0`), TTL, throttle defaults, media limit, context helpers |
| `apis/webhooks.py` | REST API: `/webhook` GET and POST only, validators, throttling, access logs, tracing, optional WAF |
| `lambdas/project_lambdas.py` | Receiver and processor Lambdas (python3.13, arm64), failure queue, event source mapping, scoped IAM |
| `lambdas/code/webhook_receiver/` | Signature check, routing by `object`, media download, conditional put |
| `lambdas/code/message_processor/` | Claims, identity resolution, transcription, agent call, replies (`lambda_function.py`, `agentcore_service.py`) |
| `layers/common/python/` | Layer sources: `security.py` (HMAC), `graph_api.py` (version, URL safety), `media_utils.py`, `message_utils.py`, `idempotency.py`, `secrets_cache.py`, `safe_logging.py`, `utils.py` |
| `layers/requirements.txt`, `layers/build_layer.sh`, `layers/project_layers.py` | Pinned layer dependencies built for aarch64 and Python 3.13 into `layers/.build` (git-ignored, hash based rebuild) |
| `databases/databases.py` | Messages table (stream, TTL, SSE) and unified users table (GSIs, PITR) |
| `tests/` | pytest: signature verification, receiver, processor, media safety, helpers, CDK assertions |

## Commands

```bash
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall lambdas layers/common tests whatsapp_api_gateway apis
export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1
npx --yes aws-cdk@latest synth --quiet      # builds the arm64 layer (needs uv and network on first run)
```

Deploy (stack 00 first): `npx aws-cdk@latest deploy`. Then put the real values in both secrets (see the README) and configure the Meta webhook with the `WebhookUrl` output.

CDK context (`-c key=value`): `graph_api_version` (`v25.0`), `media_retention_days` (30), `retain_data`, `throttle_rate_limit` (50), `throttle_burst_limit` (100), `enable_waf` (regional web ACL with a per-IP rate rule), `receiver_reserved_concurrency` and `processor_reserved_concurrency` (unset), `layer_path` (prebuilt layer folder, used by tests to skip the dependency build).

## Dependencies and environment

`aws-cdk-lib==2.275.0`. Layer: `requests==2.34.2`, `boto3==1.43.125`. SSM parameters read at deploy time: `/agentcore/agent_runtime_arn`, `/agentcore/runtime_role_arn`; this stack writes `/agentcore/unified_users_table_name` for stack 00 (do not change). Secrets Manager keys:

* WhatsApp secret: `WHATS_APP_SECRET` (Meta app secret, signature key), `WHATS_VERIFICATION_TOKEN`, `WHATS_TOKEN`, `DISPLAY_PHONE_NUMBER`.
* Instagram secret: `IG_APP_SECRET`, `IG_TOKEN`, `IG_ACCOUNT_ID`, `IG_VERIFICATION_TOKEN`.

Values starting with `CHANGE_ME` are treated as unconfigured. The `*_APP_SECRET` keys are generated randomly by the stack, so an unconfigured deployment rejects every POST.

## Conventions and gotchas

* The webhook is public because the caller is Meta. The auth gate is `X-Hub-Signature-256` (HMAC-SHA256 of the raw body, app secret from Secrets Manager, `hmac.compare_digest`). It fails closed with 403 when the secret is missing, unreadable or a placeholder, or when the header is absent or wrong, and it runs before the body is parsed. A WhatsApp signature cannot authorize an `instagram` object and the other way round. Do not add a bypass, a debug flag or an unauthenticated route. `tests/test_receiver.py` proves unsigned and invalid POSTs are rejected.
* No CORS: Meta calls server to server. Responses carry no `Access-Control-*` headers (asserted).
* Never log bodies, headers, tokens or phone numbers. Use ids, counts and `mask_phone`. Never store tokens in DynamoDB or pass them through the stream; the processor reads them from Secrets Manager.
* Tokens go in the `Authorization` header, never in URLs. Media downloads and the WhatsApp token only go to Meta owned HTTPS hosts (`graph_api.is_allowed_media_url`, override with `MEDIA_HOST_SUFFIXES`), with a size cap (`MAX_DOWNLOAD_BYTES`, 25 MB) and a 20 second budget.
* Idempotency: conditional put on `(from_phone, id)` (TTL 48 h covers Meta's 36 h retry window), then a claim per message in the processor (`proc_state`). Transient failures return 500 to Meta, which is safe because of the conditional put.
* The processor returns `state` plus `batchItemFailures` (tumbling window contract). Stream source has bisect, retry limit, record age limit and an SQS on-failure destination.
* Agent payload: `idempotency_key` (message id), `actor_id` (canonical user id from the unified users table, padded to 33 characters), session id `wa-chat-<phone>` or `ig-chat-<id>` padded to 33. The actor id header is added with a boto3 `before-sign` hook because `InvokeAgentRuntime` has no custom header parameter. Keep `[Channel: ...]` and `[UserID: ...]` tags at the start of the prompt.
* Receiver Lambda timeout stays below the 29 second API Gateway limit. Access logging uses the account level API Gateway CloudWatch role, which `RestApi(cloud_watch_role=True)` creates once per Region.
* Layer sources are copied into `layers/.build`; editing any file under `layers/common/python`, `requirements.txt` or `build_layer.sh` triggers a rebuild at synth. Tests import the layer modules straight from `layers/common/python`.

## Current state

Modernized in FEAT-003 (see `docs/AUDIT.md` section 9): HMAC webhook authentication, API Gateway hardening (no CORS, validators, throttling, access logs, tracing, optional WAF), Python 3.13 arm64 Lambdas and layer, Graph API `v25.0` from one constant, deploy-time SSM resolution, durable dedupe and claims, tokens removed from the table, scoped IAM, stream failure handling with DLQ, SSE and PITR, media lifecycle, and 72 offline tests.
