# AGENTS.md: whatsapp-ai-agent-sample-for-aws-agentcore

Single guide for coding agents working in this repository. `CLAUDE.md` is a symlink to this file; edit only `AGENTS.md`. Each stack also has its own `AGENTS.md` with the details of that stack.

## What this repository does

A sample that puts one Strands Agents multimodal agent, running on Amazon Bedrock AgentCore Runtime with AgentCore Memory, behind WhatsApp and Instagram. Three independent CDK apps (Python) deployed in order, plus notebooks to test the agent without a messaging channel.

Works with Amazon Bedrock AgentCore. Demo code: not a production reference.

## Structure

| Path | Purpose |
|---|---|
| `00-agent-agentcore/` | AgentCore Runtime (direct code deploy, Python 3.13, arm64), Memory, media bucket, TwelveLabs secret. Writes SSM `/agentcore/*`. See its `AGENTS.md`. |
| `01-whatsapp-end-user-messaging/` | WhatsApp through AWS End User Messaging Social: SNS, receiver Lambda, DynamoDB Stream tumbling window, processor Lambda, AgentCore. |
| `02-multichannel-api-gateway/` | WhatsApp and Instagram DM through one API Gateway webhook (HMAC verified), same buffering pipeline, unified users table. |
| `notebook/` | `test_agentcore_deployed.ipynb` (invokes the deployed runtime), `test_multimodal_agent.ipynb` (runs the real agent code locally without memory). Outputs are never committed. |
| `images/` | Single copy of the demo GIFs and the architecture diagram. Stack READMEs link to `../images/`; only media unique to a stack lives in its own `images/`. |
| `scripts/check_repo.py` | Repository hygiene checks, standard library only (see Commands). |
| `docs/AUDIT.md` | Live-docs audit: findings, decisions, final statuses, deferred and not validated items. Read it before changing versions or design. |

The stacks are separate CDK apps with their own `app.py`, `cdk.json`, `requirements.txt` and `requirements-dev.txt`. They share configuration only through SSM Parameter Store.

## Commands

Everything below works offline with fake credentials; nothing needs a real AWS account until `deploy`.

```bash
# Per stack (from its folder). venvs are git-ignored.
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt        # stack 00 also needs the runtime packages, see its AGENTS.md
python -m pytest -q

export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1
npx --yes aws-cdk@latest synth --quiet

# Repository root
python scripts/check_repo.py                   # placeholders, TRK links, em dashes, retired model id, README sections, notebook outputs
```

Stack 00 and stack 02 builds need `uv` and network on the first synth (the zip and the arm64 layer are built then). Deploy order: 00, then 01 or 02. Details and context options (`-c key=value`) are in each stack README.

Test counts at the end of the modernization: stack 00: 76, stack 01: 41, stack 02: 72.

## Key design decisions

### Message buffering (tumbling window)

Stacks 01 and 02 use a DynamoDB Stream with a tumbling window (20 seconds in stack 01, 10 seconds in stack 02; the `buffer_seconds` argument of the Lambda construct) so rapid-fire messages become one agent call. Table key: `from_phone` (partition), `id` (sort), so one sender stays in one shard. The processor groups by sender, joins texts with newlines and keeps the last media item. Based on [sample-whatsapp-end-user-messaging-connect-chat](https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat). The "about 75% fewer invocations" figure is that project's reported ratio, not something measured here.

### Model

Default `global.anthropic.claude-sonnet-4-6`, defined once in `00-agent-agentcore/agentcore/config.py` and mirrored in `multimodal_agent.py` (a test keeps both equal). Override with `-c model_id=...` or `MODEL_ID`. Sonnet 5.5 is opt-in. IAM follows the configured model. Claude Sonnet 4 (`us.anthropic.claude-sonnet-4-20250514-v1:0`) reached end of life on 2026-10-14; `scripts/check_repo.py` fails if that ID returns. `TL_MODEL_NAME` (default `pegasus1.2`) selects the TwelveLabs model.

### AgentCore Memory IDs

Two different strings, each padded to 33 characters with `ljust(33, "0")`:

* `actor_id`: the user. Stack 02 uses the canonical id from the unified users table (`wa-user-<phone>` or `ig-user-<id>`; once accounts are linked both channels resolve to the same id). Stack 01 uses `wa-user-<phone>`. Long-term memory is keyed by it.
* `session_id`: the conversation, `wa-chat-<phone>` or `ig-chat-<id>`. Sent as `runtimeSessionId`.

The actor id travels in the payload. Stack 02 also sends it in the `X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id` header through a boto3 `before-sign` hook, because `InvokeAgentRuntime` has no parameter for custom headers.

### Memory strategies and namespaces

Four strategies with fixed namespace templates (declared in CDK, retrieved by the agent): `/facts/{actorId}/`, `/preferences/{actorId}/`, `/summaries/{actorId}/{sessionId}/`, `/episodes/{actorId}/`. Changing one means changing `config.py` and `agent_helpers.py` together. Event expiry defaults to 30 days (`memory_expiry_days`). The old `/strategies/{id}/actors/...` format and the strategy id environment variables no longer exist.

### One agent per request

A runtime microVM serves several sessions, and Strands raises `ConcurrencyException` for overlapping calls on one instance. The agent is therefore built per request with its own session manager, in a `with` block. Never reintroduce a module-global agent. The model client is shared (stateless).

### Unified user identity (stack 02)

`unified_users` maps channel ids to a canonical `user_id` (GSIs `wa-phone-index` and `ig-id-index`). The agent has a `link_account` tool to merge records. Stack 00's runtime role reads the table name from SSM `/agentcore/unified_users_table_name`, which stack 02 writes after stack 00 exists. When linked from WhatsApp by Instagram username, the numeric `ig_id` is unknown until Instagram sends a webhook; the processor then looks up by `ig_username` and backfills `ig_id`. `link_account` trusts identifiers typed in chat (`LINK_ALLOW_MERGE=false` disables merging).

### Prompt tags

The processor starts every prompt with `[Channel: whatsapp|instagram]`, and in stack 02 `[UserID: ...]` and `[User: name]`. The agent offers account linking only when `[UserID:]` exists.

### Multimedia

AgentCore Memory only stores text, so media becomes text first. Image: Claude vision (inline block). Audio: Amazon Transcribe in the Lambda, transcript sent as text. Video: TwelveLabs API called directly from the `video_analysis` tool (key in Secrets Manager, `TL_SECRET_ARN`); the agent writes `[VIDEO: id=... | desc="..."]` tags for follow-ups. Document: inline block with a sanitized file name.

Critical rules: the text block must be the first content block (the memory session manager reads `content[0]["text"]`); media is validated before it reaches the agent (invalid content in memory cannot be removed per record, the memory resource has to be recreated); decoded media is limited to about 1.5 MB.

### Security model

* Stack 02 webhook: public because Meta calls it; the gate is `X-Hub-Signature-256` (HMAC-SHA256 over the raw body, app secrets `WHATS_APP_SECRET` and `IG_APP_SECRET` in Secrets Manager), constant time, fail closed. No CORS. No bypass flag.
* Stack 01 has no public endpoint; SNS delivery is IAM authenticated and the receiver checks the topic ARN.
* Logs hold ids, counts and exception types only; never message text, phone numbers (use `mask_phone`), tokens or provider error bodies. Tokens are never stored in DynamoDB or put in URLs.
* IAM has no `Resource: "*"` except X-Ray write and `cloudwatch:PutMetricData` (namespace conditioned), which do not support resource scoping. Inference profiles need the foundation model ARN with a Region wildcard (documented requirement).

### Runtime sessions

A session runs in an isolated microVM for up to 8 hours and ends after 15 minutes idle. After redeploying agent code, wait for idle timeout or use a new `session_id`. The agent must end with `app.run()`; do not exclude packages from the zip; do not put `bedrock-agentcore-starter-toolkit` in the runtime requirements (it is a CLI).

### SSM contract between stacks

Written by stack 00: `/agentcore/agent_runtime_arn`, `/agentcore/s3_bucket_name`, `/agentcore/memory_id`, `/agentcore/runtime_role_arn`. Written by stack 02: `/agentcore/unified_users_table_name`. Stacks 01 and 02 read them at deploy time through CloudFormation parameters (`StringParameter.value_for_string_parameter`), so `cdk synth` needs no credentials. Do not reintroduce synth-time boto3 calls. Do not rename these parameters.

### Observability

The runtime emits OpenTelemetry through `aws-opentelemetry-distro`; logs go to CloudWatch Logs. Traces appear only after CloudWatch Transaction Search is enabled once per account. Lambdas have active X-Ray tracing and 30 day log groups.

## Dependencies, accounts and environment

Pins live in each stack (`requirements.txt`, `requirements-dev.txt`, `agent_files/requirements.txt`, `layers/requirements.txt`); the version table is in `docs/AUDIT.md` section 3 (`aws-cdk-lib==2.275.0`, `strands-agents[otel]==1.64.0`, `bedrock-agentcore[strands-agents]==1.25.3`). Python 3.11+ on the dev machine; runtime and Lambdas use 3.13 on arm64. Node for `npx aws-cdk@latest`. `uv` is required by the builds.

Needed in the account: Bedrock model access, CDK bootstrap, Amazon Bedrock AgentCore, and for the channels either End User Messaging Social (stack 01) or a Meta app (stack 02). TwelveLabs API key optional.

Secrets (values never in the repo): stack 00 TwelveLabs `TL_API_KEY`; stack 02 WhatsApp secret `WHATS_APP_SECRET`, `WHATS_VERIFICATION_TOKEN`, `WHATS_TOKEN`, `DISPLAY_PHONE_NUMBER` and Instagram secret `IG_APP_SECRET`, `IG_TOKEN`, `IG_ACCOUNT_ID`, `IG_VERIFICATION_TOKEN`. Values starting with `CHANGE_ME` count as unconfigured.

Environment variables of the runtime and the Lambdas are listed in the stack `AGENTS.md` files.

## Conventions and gotchas

* Documentation rules: no em dashes, no TL;DR section, TRK parameters (`?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el`) on links to aws.amazon.com, docs.aws.amazon.com, community.aws, builder.aws.com, strandsagents.com and kiro.dev, README sections Contributing, Security and License, inclusive language (allowlist, denylist, primary, replica). `python scripts/check_repo.py` enforces the mechanical ones.
* Notebooks are committed without outputs. Do not tell users to run `jupyter notebook <path>`; say to open the notebook in JupyterLab or VS Code.
* Generated and ignored: `.venv`, `cdk.out`, `agent_files/deployment_package*`, `layers/.build*`, `*_executed.ipynb`. Do not commit them.
* Do not rewrite git history to shrink the repository; the GIFs were deduplicated by keeping one copy in `images/`.
* The sample uses PUBLIC runtime network mode with SigV4 (server to server). Gateway, Identity, Policy and Evaluations are documented as next steps, not deployed.

## Current state and recent changes

Branch `feat/modernize-agentcore-strands` (see `docs/AUDIT.md` for the full list and the deferred items):

* Stack 00: Sonnet 4.6 default, Python 3.13 runtime, pinned dependencies, per-request agents, limits, optional Guardrail, tool-safety plugin, four memory strategies with namespaces, scoped IAM, media lifecycle.
* Stacks 01 and 02: Python 3.13 arm64 Lambdas, Graph API `v25.0`, HMAC webhook verification, API Gateway hardening, durable dedupe and claims, tokens out of DynamoDB, scoped IAM, stream failure handling, deploy-time SSM resolution, offline tests.
* Docs and repo: root `AGENTS.md` (this file) replaces the old `CLAUDE.md`, refreshed READMEs, notebooks updated and stripped of outputs, duplicate GIFs removed, `scripts/check_repo.py`.
* Not validated against real services (no AWS account, Meta app or TwelveLabs key in the sandbox): see `docs/AUDIT.md`, section "Deferred / not validated offline".
