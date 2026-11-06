# AGENTS.md: 00-agent-agentcore

## What this app does

CDK app (Python) that deploys the AI brain of the WhatsApp/Instagram sample: an Amazon Bedrock AgentCore Runtime (direct code deploy, Python 3.13, ARM64) running a Strands Agents multimodal agent, plus an AgentCore Memory (facts, preferences, summaries, episodes), an S3 media bucket, and a Secrets Manager entry for the TwelveLabs key. Stacks `01-whatsapp-end-user-messaging` and `02-multichannel-api-gateway` invoke the runtime.

## Structure

| Path | Purpose |
|---|---|
| `app.py`, `cdk.json` | CDK entry point (stack `AgentAgentCoreStack`) |
| `agent_agentcore/agent_agentcore_stack.py` | Stack: secret, bucket (media lifecycle), memory, role, runtime, endpoint, SSM parameters, outputs |
| `agentcore/config.py` | Single source of truth: default model, memory namespaces, ARN helpers |
| `agentcore/agentcore_memory.py` | `CfnMemory` with 4 strategies, each with a namespace template; memory execution role |
| `agentcore/agentcore_role.py` | Least-privilege runtime execution role |
| `agentcore/agentcore_deployment.py` | `CfnRuntime`; hash-based rebuild of the zip via `create_deployment_package.sh` |
| `agent_files/multimodal_agent.py` | Runtime entrypoint (`BedrockAgentCoreApp`), settings, prompt, per-request agent |
| `agent_files/agent_helpers.py` | Pure helpers: payload validation, ID rules, redaction, namespaces, duplicate cache, JSON logs |
| `agent_files/agent_hooks.py` | Strands `Plugin`: tool-call logging and tool-result redaction |
| `agent_files/video_analysis_tool.py` | TwelveLabs tool (validated input, timeouts, cached key, generic errors) |
| `agent_files/link_account_tool.py` | Cross-channel identity linking tool (DynamoDB) |
| `tests/` | pytest: helpers, agent wiring, tools, CDK assertions |

## Commands

Install (use a venv in this folder, it is git-ignored):

```bash
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt \
  "bedrock-agentcore[strands-agents]==1.25.3" "strands-agents[otel]==1.64.0" \
  twelvelabs==1.3.8 aws-opentelemetry-distro==0.22.0 requests==2.34.2
```

Test and verify (no AWS credentials needed):

```bash
python -m pytest -q
python -m compileall agent_files agentcore agent_agentcore tests
export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1
npx --yes aws-cdk@latest synth --quiet     # runs create_deployment_package.sh (needs uv and network)
```

Deploy: `npx aws-cdk@latest deploy` (needs `uv` on PATH; the zip is built for `aarch64-manylinux2014`, Python 3.13). Then store the TwelveLabs key:

```bash
aws secretsmanager put-secret-value --secret-id <TwelveLabsSecretArn output> \
  --secret-string '{"TL_API_KEY":"<your key>"}'
```

CDK context options (`-c key=value`): `model_id`, `memory_expiry_days` (3 to 365, default 30), `enable_summary_memory`, `enable_episodic_memory`, `guardrail_id`, `guardrail_version`, `unified_traces_destination`, `media_retention_days` (default 30), `package_path` (prebuilt zip, used by tests).

## Dependencies and environment

Pinned in `agent_files/requirements.txt` (runtime zip) and `requirements.txt` (CDK). Versions come from `docs/AUDIT.md` section 3.

Runtime environment variables set by the stack: `AWS_REGION`, `MODEL_ID`, `BEDROCK_AGENTCORE_MEMORY_ID`, `S3_BUCKET`, `TL_SECRET_ARN`, `TL_MODEL_NAME`, optional `BEDROCK_GUARDRAIL_ID`/`BEDROCK_GUARDRAIL_VERSION`, `UNIFIED_TRACES_DESTINATION_ENABLED`.
Optional agent tuning (set manually or via a new `environment_variables` entry): `ENABLE_PROMPT_CACHING` (default true), `AGENT_MAX_TURNS` (12), `AGENT_MAX_TOTAL_TOKENS` (200000), `AGENT_WINDOW_SIZE` (40), `LOG_LEVEL`, `LINK_ALLOW_MERGE` (true), `TL_INDEXING_TIMEOUT_SECONDS` (540).

Accounts: Bedrock model access for the chosen model, CDK bootstrap, optional TwelveLabs API key. For traces, enable CloudWatch Transaction Search once per account ([docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)).

## Conventions and gotchas

* The cross-stack contract must not change: SSM parameters `/agentcore/agent_runtime_arn`, `/agentcore/s3_bucket_name`, `/agentcore/memory_id`, `/agentcore/runtime_role_arn` and the stack outputs. Stack 01/02 also read `/agentcore/unified_users_table_name` (written by stack 02) in `link_account_tool.py`. `tests/test_stack.py` asserts the contract.
* Default model is `global.anthropic.claude-sonnet-4-6`. It lives in `agentcore/config.py` and `multimodal_agent.py`; `tests/test_namespaces.py` keeps both equal. Sonnet 5.5 is opt-in through `model_id`; IAM scoping follows the model automatically.
* Memory namespaces are fixed templates (`/facts/{actorId}/`, `/preferences/{actorId}/`, `/summaries/{actorId}/{sessionId}/`, `/episodes/{actorId}/`) declared in CDK and retrieved by the agent. If you change one, change `config.py` and `agent_helpers.py` together (a test enforces this).
* One Strands `Agent` is built per request and its session manager is closed by a `with` block. Never reintroduce a module-global agent: a runtime microVM serves several sessions and Strands raises `ConcurrencyException` for overlapping calls on one instance.
* Actor and session IDs are padded to 33 characters with `ljust(33, "0")` (same rule as the Lambdas). Keep the text block FIRST in multimodal messages (the memory session manager reads `content[0]["text"]`).
* Logs are JSON and contain hashed IDs only, never message text, phone numbers, URLs with signatures, or provider error bodies. Errors shown to the model or user are generic.
* IAM: no `Resource: "*"` except X-Ray write and `cloudwatch:PutMetricData` with the `bedrock-agentcore` namespace condition (neither supports resource scoping). Bedrock inference profiles need the foundation model ARN with a Region wildcard (documented requirement).
* Changing anything in `agent_files/*.py`, `requirements.txt` or `create_deployment_package.sh` triggers an automatic zip rebuild (hash in `agent_files/deployment_package.sha256`). The generated zip, `deployment_package/` and the hash file are git-ignored.
* Do not add `bedrock-agentcore-starter-toolkit` to the runtime requirements (it is a CLI).
* `link_account` trusts identifiers typed in chat. For production add out-of-band verification, or set `LINK_ALLOW_MERGE=false`.

## Current state

Modernized in FEAT-002 (see `docs/AUDIT.md`): Sonnet 4.6 default, Python 3.13 runtime, pinned dependencies, per-request agents with explicit conversation manager, limits, prompt caching, optional Guardrail, tool-safety plugin, JSON logs and trace attributes, summary and episodic memory with namespaces, 30 day event expiry, scoped IAM with confused-deputy conditions, request header allowlist, media lifecycle rules, generated TwelveLabs placeholder secret, and 76 offline tests.
Not changed on purpose: PUBLIC network mode with SigV4 (server to server invocation), no Gateway/Identity/Evaluations resources (listed as next steps in the audit).
