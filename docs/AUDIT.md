# Modernization audit

Audit date: 2026-11-06 (sandbox clock). Branch: `feat/modernize-agentcore-strands` (base `main` @ `00fde04`).
Scope: `00-agent-agentcore`, `01-whatsapp-end-user-messaging`, `02-multichannel-api-gateway`, `notebook/`, root docs.

Every "Reference" below is a page that was fetched live during this audit (not recalled from memory). Status values: `todo` (to be done in FEAT-002..004), `info` (no change needed, recorded for context).

Owner features: FEAT-002 = stack 00 (agent + AgentCore infra), FEAT-003 = stacks 01 and 02, FEAT-004 = docs, notebooks, tests, repo hygiene.

## 1. Executive summary

The sample is stale in ways that make it fail or degrade today, not only cosmetically:

1. The default model `us.anthropic.claude-sonnet-4-20250514-v1:0` (Claude Sonnet 4) reached **EOL on October 14, 2026**. Invocations fail.
2. The AgentCore Runtime is created with `PYTHON_3_11`. Direct code deploy deprecates 3.11 on **June 30, 2026** and blocks updates on **August 31, 2026**. New or updated runtimes on 3.11 are blocked.
3. Stack 01 Lambdas use `python3.11` (Amazon Linux 2, deprecation June 30, 2027) and stack 01 defaults to Graph API `v20.0`, which **expired September 24, 2026**. Stack 02 uses `v21.0` (expires January 21, 2027).
4. The Meta webhook receivers (stack 02 `webhook_receiver`, stack 01 handler path) do not verify `X-Hub-Signature-256`; anyone who finds the API Gateway URL can inject messages that then reach the agent and burn Bedrock tokens.
5. The agent keeps a module-level `_agent` keyed by a single `_current_session`. Under concurrent sessions in one microVM it either rebuilds the agent per switch or cross-wires memory. Strands raises `ConcurrencyException` for overlapping calls on one instance by default.
6. IAM uses `Resource: "*"` for AgentCore, Bedrock, SNS-adjacent and social-messaging actions, plus broad AWS managed policies (`CloudWatchLogsFullAccess`).
7. Dependencies are unpinned, and `boto3` is imported by `get_param.py` at synth time but is not declared in the stacks' `requirements.txt`.

## 2. Baseline (before any change)

Environment: Python 3.11.15, node 22, `uv`, AWS CDK CLI 2.1148.0 via `npx aws-cdk@latest` (global `npm i -g` fails in the sandbox with ENOENT on the nvm prefix, `npx` works). Fake credentials (`AWS_ACCESS_KEY_ID=x`), `CDK_DEFAULT_ACCOUNT=123456789012`, `CDK_DEFAULT_REGION=us-east-1`. `aws-cdk-lib` resolved to 2.275.0.

| Stack | `compileall` | `cdk synth` | Notes |
|---|---|---|---|
| 00-agent-agentcore | pass | **pass** | Synth runs `create_deployment_package.sh` (uv, aarch64, py3.11) and writes `agent_files/deployment_package.zip`. Both are git-ignored. Output only warns "83 feature flags are not configured". |
| 01-whatsapp-end-user-messaging | pass | **fail** | `ArnsMustStartWithArn: ARNs must start with "arn:"`. `get_param.py` returns `""` when SSM is unreachable and the stack passes it to an ARN-consuming construct. Without `boto3` installed it fails earlier with `ModuleNotFoundError: boto3` (undeclared dependency). |
| 02-multichannel-api-gateway | pass | **fail** | Same two failures as 01. |

Conclusion: stacks 01 and 02 cannot be synthesized offline or in CI because they resolve cross-stack values with live SSM calls at synth time. Plan: read via `ssm.StringParameter.value_for_string_parameter` (deploy-time CloudFormation parameter resolution) or accept context/env overrides, so synth needs no credentials. `git status` was clean after the baseline run (generated artifacts are ignored).

## 3. Versions chosen

Checked with `pip index versions` on 2026-11-06.

| Package | Latest on PyPI | Pin to use | Where |
|---|---|---|---|
| `bedrock-agentcore` | 1.25.3 | `bedrock-agentcore[strands-agents]==1.25.3` | 00 agent_files |
| `strands-agents` | 1.64.0 | `strands-agents[otel]==1.64.0` | 00 agent_files |
| `strands-agents-tools` | 0.9.0 | `strands-agents-tools==0.9.0` (only if a tool from it is used; otherwise drop) | 00 agent_files |
| `twelvelabs` | 1.3.8 | `twelvelabs==1.3.8` | 00 agent_files |
| `aws-opentelemetry-distro` | 0.22.0 | `aws-opentelemetry-distro==0.22.0` | 00 agent_files |
| `boto3` | 1.43.125 | `boto3==1.43.125` (stack 02 layer, CDK `get_param` removal makes it optional for synth) | 02 layer |
| `requests` | 2.34.2 | `requests==2.34.2` | 02 layer |
| `aws-cdk-lib` | 2.275.0 | `aws-cdk-lib==2.275.0` | 00, 01, 02 |
| `constructs` | 10.8.1 | `constructs>=10.4.2,<11.0.0` | 00, 01, 02 |
| `pytest` | 9.2.0 | `pytest==9.2.0` in `requirements-dev.txt` | per stack |
| `bedrock-agentcore-starter-toolkit` | 0.3.14 | remove from runtime requirements (it is a CLI, not a runtime dependency; AgentCore CLI is now `npm install -g @aws/agentcore`) | 00 |

Other decisions:

| Item | Choice | Why |
|---|---|---|
| AgentCore Runtime language runtime | `PYTHON_3_13` (zip built with `--python-version 3.13 --python-platform aarch64-manylinux2014`) | 3.14 is supported but wheels for some deps (twelvelabs, otel distro) lag; 3.13 deprecates 2029-06-30. |
| Lambda runtime | `python3.13`, `Architecture.ARM_64` | Amazon Linux 2023; Lambda supports arm64 on all runtimes. Layer in stack 02 must be built for arm64 and 3.13. |
| Default model | `global.anthropic.claude-sonnet-4-6` | Active, 1M context, adaptive thinking off by default (lower latency for chat), also Strands' documented default. Sonnet 5.5 (`global.anthropic.claude-sonnet-5-5`, launched 2026-09-28, EOL no sooner than 2027-09-28) is documented as opt-in via `MODEL_ID`: it has adaptive thinking on by default and thinking blocks bound to the account. Provide a geo alternative (`us.` / `eu.`) for data residency. |
| Graph API version | `v25.0` for WhatsApp and Instagram, one shared constant, overridable by env | `v25.0` expires 2028-07-29 (see table below); `v26.0` is newer but has no announced expiration yet. |

## 4. Findings

Severity: **Critical** (breaks or exposes today), **High**, **Medium**, **Low**.

### 4.1 Models, runtimes and dependencies

| ID | Area | Current state | Reference | Sev | Action | Status |
|---|---|---|---|---|---|---|
| A-01 | Bedrock model ID | `MODEL_ID` defaults to `us.anthropic.claude-sonnet-4-20250514-v1:0` in `multimodal_agent.py`, `agent_agentcore_stack.py`, READMEs, CLAUDE.md, notebooks. Claude Sonnet 4 legacy date 2026-04-14, **EOL 2026-10-14**. README also suggests a non-existent `claude-sonnet-4-6-20250514` ID. | [Model lifecycle (Legacy)](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html), [Sonnet 4.6 card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html), [Sonnet 5.5 card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html), [Strands Bedrock provider](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/amazon-bedrock/) | Critical | Default to `global.anthropic.claude-sonnet-4-6`; single source of truth for default; document 5.5 and geo IDs; scope IAM to the chosen model and inference profile ARNs. | done (FEAT-002) |
| A-02 | AgentCore Runtime language | `runtime="PYTHON_3_11"` in `agentcore_deployment.py`. | [Supported direct-deploy runtimes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html) | Critical | `PYTHON_3_13`; update `create_deployment_package.sh` to `--python-version 3.13`. | done (FEAT-002) |
| A-03 | Lambda runtime and architecture | Stack 01: `python3.11` (AL2, deprecates 2027-06-30). Stack 02: `python3.12`. x86_64 default. | [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) | High | `python3.13` + `ARM_64` everywhere; rebuild layer for arm64/3.13. | done (FEAT-003): `python3.13` + `ARM_64` on every Lambda in stacks 01 and 02; layer built for aarch64/3.13 by `layers/build_layer.sh` |
| A-04 | Dependency pinning | Agent `requirements.txt` unpinned except `strands-agents[otel]>=1.27.0`; stacks use `aws-cdk-lib>=2.170.0`; `boto3` undeclared though `get_param.py` imports it. | PyPI queries in section 3 | High | Pin exact versions from section 3; add `requirements-dev.txt`; declare/remove boto3. | done: stack 00 (FEAT-002); stacks 01/02 (FEAT-003): `aws-cdk-lib==2.275.0`, layer pins `requests==2.34.2` and `boto3==1.43.125`, `requirements-dev.txt`, `get_param.py` and its synth-time boto3 import removed |
| A-05 | Graph API versions | Stack 01 `v20.0` (expired 2026-09-24), stack 02 `v21.0` (expires 2027-01-21) for WhatsApp, `v24.0` for Instagram, one hardcoded `v21.0` URL in `webhook_receiver`. | [Graph API versions](https://developers.facebook.com/docs/graph-api/changelog/versions) | Critical (01), High (02) | One constant `GRAPH_API_VERSION="v25.0"` (env override) used by both channels and the shared layer. | done (FEAT-003): `v25.0` from one constant per stack (`config.py`, CDK context `meta_api_version` / `graph_api_version`), passed to the Lambdas as an env var; Instagram unified on the same version |
| A-06 | Stale tooling in requirements | `bedrock-agentcore-starter-toolkit` shipped inside the runtime zip. | [Strands on AgentCore (Python)](https://strandsagents.com/latest/documentation/docs/user-guide/deploy/deploy_to_bedrock_agentcore/python/) (CLI is `@aws/agentcore`) | Low | Remove from deploy package; mention CLI in docs only. | done (FEAT-002) |

### 4.2 Strands Agents (agent code)

| ID | Area | Current state | Reference | Sev | Action | Status |
|---|---|---|---|---|---|---|
| S-01 | Global agent cache | `_agent` and `_current_session` module globals. A second session in the same microVM discards the first agent (and its session manager); two overlapping invocations on one instance raise `ConcurrencyException` (default `THROW`). | [Agent loop: concurrent invocations](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/agent-loop/) | High | Build one agent per request (cheap) or an LRU keyed by `(actor_id, session_id)` with a per-key lock. Never share an instance across sessions. Make construction a pure function so it is unit-testable. | done (FEAT-002) |
| S-02 | Session manager lifecycle | `AgentCoreMemorySessionManager` created but never closed. | [AgentCore Memory session manager](https://strandsagents.com/latest/documentation/docs/community/session-managers/agentcore-memory/) | Medium | Use `with AgentCoreMemorySessionManager(...) as sm:` so buffered messages flush; keep `batch_size=1` for chat. | done (FEAT-002) |
| S-03 | Retrieval namespaces | `retrieval_config` keys built as `/strategies/{id}/actors/{actor}/` from `FACTS_STRATEGY_ID`/`PREFERENCES_STRATEGY_ID` env vars that are unknown until after the memory is created (chicken and egg), values are plain dicts. | [Strands AgentCore memory: retrieval and namespace patterns](https://strandsagents.com/latest/documentation/docs/community/session-managers/agentcore-memory/), [Memory organization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-organization.html) | High | Declare namespaces on each strategy in CDK (`/facts/{actorId}/`, `/preferences/{actorId}/`, `/summaries/{actorId}/{sessionId}/`, `/episodes/{actorId}/`) and reference the same strings with `RetrievalConfig(top_k, relevance_score)`. Remove the strategy ID env vars. | done (FEAT-002) |
| S-04 | Conversation management | Default sliding window only; large multimodal tool results can overflow. | [Conversation management](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/conversation-management/) | Medium | Explicit `SlidingWindowConversationManager(window_size, should_truncate_results=True)`; document `SummarizingConversationManager` as an option. | done (FEAT-002) |
| S-05 | Cost and runaway guards | No limits on turns or tokens per invocation. | [Agent loop: invocation limits](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/agent-loop/) | Medium | Pass `limits={"turns":..., "total_tokens":...}` and handle `stop_reason` (`limit_turns`, `limit_total_tokens`) with a polite fallback message. Env-configurable. | done (FEAT-002) |
| S-06 | Retried webhooks and duplicate invocations | Same user message can reach the agent twice (Lambda retry, stream replay). | [Agent loop: idempotency_token](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/agent-loop/) | Medium | Pass `idempotency_token` derived from the WhatsApp/Instagram message ids; also dedupe at ingestion (see W-05). | done: agent cache (FEAT-002) + durable ingestion dedupe and per-message claims (FEAT-003); the Lambdas send the message id as payload `idempotency_key` |
| S-07 | Hooks for guardrails/telemetry | None. System prompt carries all "do not leak" rules. | [Hooks](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/hooks/) | Medium | Add a small `Plugin` with hooks for tool-call logging and a tool-result redaction step (strip S3 URIs/ARNs from error text before it reaches the model). Keep prompt rules as defense in depth. | done (FEAT-002) |
| S-08 | Streaming entrypoint | `invoke` is sync and returns `str(result)` (includes repr noise for non-text blocks). | [Strands on AgentCore: streaming example](https://strandsagents.com/latest/documentation/docs/user-guide/deploy/deploy_to_bedrock_agentcore/python/) | Low | Return `result.message` text; keep sync (callers read a full reply for WhatsApp) but extract text blocks explicitly. | done (FEAT-002) |
| S-09 | Observability | `strands-agents[otel]` and `aws-opentelemetry-distro` listed but no explicit wiring or span attributes (actor, session, channel). | [Strands traces](https://strandsagents.com/latest/documentation/docs/user-guide/observability-evaluation/traces/), [AgentCore observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html) | Medium | Document and enable AgentCore Observability (CloudWatch GenAI dashboard, Transaction Search); set `trace_attributes` with channel/session; ensure the execution role can write logs/spans; run entrypoint via `opentelemetry-instrument` where applicable. | done (FEAT-002) |
| S-10 | Model config | `BedrockModel(model_id=...)` only. | [Strands Bedrock provider](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/amazon-bedrock/) (guardrails, prompt caching supported) | Medium | Expose optional `BEDROCK_GUARDRAIL_ID/VERSION` and prompt caching of the (long) system prompt via env flags; default off for guardrail, on for caching where the model supports it (min 1,024 tokens on Sonnet 4.6). | done (FEAT-002) |
| S-11 | Payload media limits | `MAX_MEDIA_BYTES = 1.5 MB` compares the length of the base64 string, not decoded bytes; message says "under 1.5 MB". | repo code | Low | Compare decoded size or fix the message; add unit tests for `_sanitize_document_name`, `_validate_media`, `build_multimodal_prompt`. | done (FEAT-002); tests for 01/02 logic FEAT-004 |

### 4.3 AgentCore (infrastructure and platform use)

| ID | Area | Current state | Reference | Sev | Action | Status |
|---|---|---|---|---|---|---|
| C-01 | CDK constructs | L1 `CfnRuntime`/`CfnMemory` from `aws-cdk-lib` 2.275.0. Introspection of the installed package shows `CfnRuntime` supports `authorizer_configuration` (custom JWT), `request_header_configuration`, `protocol_configuration`, `session storage/filesystem`, `platform_version`, and `CfnMemory` supports Semantic, UserPreference, Summary, Episodic, Custom/self-managed strategies, plus stream delivery. Also available: `CfnGateway`, `CfnWorkloadIdentity`, `CfnPolicyEngine`, `CfnOnlineEvaluationConfig`, `CfnEvaluator`. No L2 in `aws-cdk-lib` (alpha package is not on PyPI). | `python -c "import aws_cdk.aws_bedrockagentcore"` against 2.275.0 | Info | Keep L1; use the new properties instead of waiting for L2. | info |
| C-02 | Memory strategies | Only Semantic (`UserFacts`) and UserPreference. No Summary or Episodic. | [Memory strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html), [Built-in strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-strategies.html) | Medium | Add `SummaryMemoryStrategy` and `EpisodicMemoryStrategy` with explicit namespaces (S-03). Keep the prompt-side "state the fact explicitly" guidance for multimodal text. | done (FEAT-002) |
| C-03 | Short-term memory expiry | `MEMORY_EXPIRY_DAYS = 3` for events. A user returning after the weekend loses recent turns (long-term records are separate). | [Memory organization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-organization.html) | Medium | Raise default to 30 days, configurable via context (`memory_expiry_days`). Comment tradeoff (cost vs recall). | done (FEAT-002) |
| C-04 | Runtime network and auth | `network_mode="PUBLIC"`, IAM SigV4 only, no `authorizer_configuration`. Invoked server-to-server from Lambda with SigV4, which is the recommended default for this topology. | [Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html), [Inbound/outbound auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html) | Medium | Keep SigV4 (no public client), document why; scope invoke permission to the runtime ARN; optional context flag for VPC mode documented only. Do not add JWT authorizer (no end-user IdP in this design). | done: runtime side (FEAT-002); Lambda invoke permission scoped to the runtime ARN and `/runtime-endpoint/*` (FEAT-003) |
| C-05 | `runtimeUserId` / `InvokeAgentRuntimeForUser` | Lambdas pass `runtimeUserId=actor_id` and hold `InvokeAgentRuntimeForUser` on `*`. The doc says user-id is an unverified opaque identifier; only trusted principals should have the permission, scoped to the runtime, and the id must derive from authenticated context. | [Inbound/outbound auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html), [Security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html) | High | Scope both actions to `agent_runtime_arn` (and `/runtime-endpoint/*`); actor id is derived server-side from the verified webhook sender, never from payload text. Keep the custom actor header path in the agent as a fallback. | done: runtime side (FEAT-002); `InvokeAgentRuntimeForUser` scoped to the runtime (FEAT-003); actor id derived server side from the verified sender or the unified users table; custom actor header sent through a boto3 `before-sign` hook |
| C-06 | Execution role | `CloudWatchLogsFullAccess`, `AWSXRayDaemonWriteAccess`, `AmazonEC2ContainerRegistryReadOnly` managed policies (ECR not needed for zip deploy); `bedrock:*Invoke*` on `*`; `aws-marketplace:Subscribe` on `*`; AgentCore data-plane actions on `*`; `logs:*` on `*`. | [Security best practices (least privilege)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html) | High | Replace with inline statements scoped to: the memory ARN, the chosen model/inference-profile ARNs, `/aws/bedrock-agentcore/runtimes/*` log groups, specific S3 bucket and secret. Drop ECR and Marketplace statements unless a test proves they are needed. | done (FEAT-002) |
| C-07 | Memory execution role | `bedrock:InvokeModel*` and AgentCore data-plane actions on `*`; the memory role only needs model invocation for extraction. | same as C-06 | Medium | Limit to the extraction model ARN(s) and the memory ARN. | done (FEAT-002) |
| C-08 | Runtime lifecycle | `idle_runtime_session_timeout=900`, `max_lifetime=28800`. Reasonable. | [Code deploy concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html) | Info | Keep; expose via CDK context. Note: direct code is limited to 250 MB package and allows 25 new sessions/s. | info |
| C-09 | Gateway / Identity / Policy / Evaluations | Not used. Video tool calls TwelveLabs directly with a Secrets Manager key; account linking is a local tool backed by DynamoDB. | [Runtime service contract (protocols)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html) | Low | Out of scope for the sample's behavior; document as "next steps" (Gateway for tools, Evaluations for online scoring) rather than adding resources that raise cost. | info |
| C-10 | Deployment package | Built by shelling out from the CDK constructor if the zip is missing (stale zip is reused silently). | [Direct code deployment (Python)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html) | Medium | Always rebuild when sources or `requirements.txt` change (hash-based), exclude tests, set POSIX permissions, fail synth with a clear error. | done (FEAT-002) |
| C-11 | TwelveLabs secret | Secret created with literal `CHANGE_ME` via `SecretValue.unsafe_plain_text`. | repo code | Medium | Create the secret without a value in template (generated placeholder) and document `aws secretsmanager put-secret-value`; keep the agent reading it at runtime. | done (FEAT-002) |

### 4.4 Webhooks, API Gateway, Lambdas (stacks 01 and 02)

| ID | Area | Current state | Reference | Sev | Action | Status |
|---|---|---|---|---|---|---|
| W-01 | Webhook authenticity (stack 02) | `webhook_receiver` handles GET verify token and trusts any POST body. | [Meta webhooks: event notifications carry `X-Hub-Signature-256`](https://developers.facebook.com/docs/graph-api/webhooks/getting-started) | Critical | Verify HMAC-SHA256 of the **raw body** with the app secret (constant-time compare) before parsing; reject with 401/403 and no processing. Store app secrets in the existing Secrets Manager entries. Unit-test good/bad/missing signature and base64 bodies. | done (FEAT-003): `X-Hub-Signature-256` HMAC-SHA256 over the raw body, constant time, fail closed (403) before parsing; placeholder/random app secrets can never verify; 27 tests incl. unsigned POST = 403 |
| W-02 | Webhook path (stack 01) | Messages arrive from AWS End User Messaging Social via SNS; authenticity is provided by the AWS service/IAM, not Meta HMAC. SNS subscription is same-account Lambda. | [What is AWS End User Messaging Social](https://docs.aws.amazon.com/social-messaging/latest/userguide/what-is-service.html) | Info | No Meta HMAC needed. Confirm the SNS topic policy only allows `social-messaging.amazonaws.com` and the account; add `source_account` conditions. Keep sender allow-listing optional. | done (FEAT-003): topic policy limited to `social-messaging.amazonaws.com` with `aws:SourceAccount`; optional `aws:SourceArn` through context `topic_source_arn_pattern`; handler also checks `EventSource` and `TopicArn` and fails closed. Not verified live: the user guide pages for event destination policies were not retrievable as text, so the account condition is the default and the ARN condition is opt in |
| W-03 | API Gateway hardening | CORS preflight `allow_origins=["*"]`, `allow_headers=["*"]`, `Access-Control-Allow-Origin: *` in every Lambda response; no throttling, usage plan, WAF, access logs, or tracing. A webhook called server-to-server by Meta needs no CORS. | [Meta webhooks](https://developers.facebook.com/docs/graph-api/webhooks/getting-started) | High | Remove CORS and the `*` header; add stage throttling (rate/burst), access logging to a log group, X-Ray tracing, request size awareness (API GW 10 MB), and an optional regional WAF web ACL with a rate-based rule behind a context flag. | done (FEAT-003): CORS and wildcard header removed, stage throttling (50 rps / 100 burst, configurable), request parameter validation (POST requires the signature header, GET the three `hub.*` parameters), JSON access logs without headers or bodies, X-Ray, optional regional WAF with a per-IP rate rule (`-c enable_waf=true`) |
| W-04 | Logging of secrets and PII | `logger.info("Received event: %s", json.dumps(event))` logs full webhook payloads (phone numbers, message text, headers). | repo code | High | Log message ids and counts only; mask phone numbers; never log headers or bodies. | done (FEAT-003): JSON logs with message ids, counts, status codes and exception types only; phone numbers masked to the last four characters; no bodies, headers or provider error text; asserted by a test |
| W-05 | Idempotency | Meta retries failed deliveries; the DynamoDB item key already uses the message id but writes are unconditional and the stream replays on Lambda errors. | [Strands idempotency](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/agent-loop/) | High | `PutItem` with `attribute_not_exists(id)` for ingestion; processor skips ids already marked processed; return 200 to Meta fast. | done (FEAT-003): conditional put on `(from_phone, id)` at ingestion (TTL 48 h covers Meta's 36 h retries); processor claims each message with a conditional update (`proc_state` processing/done, stale lock takeover) and releases on failure |
| W-06 | DynamoDB stream consumer | Tumbling window with `batch_size=1000`, no `retry_attempts`, `bisect_batch_on_error`, `on_failure` destination, or `report_batch_item_failures`. A poison record retries until the table's 24 h stream retention. | [Lambda event source / DynamoDB](https://docs.aws.amazon.com/lambda/latest/dg/with-ddb.html) (page fetched; tumbling and error-handling options verified against installed `aws_lambda_event_sources.DynamoEventSource` signature) | High | Set `retry_attempts`, `bisect_batch_on_error=True`, SQS `on_failure` DLQ, `max_record_age`, `report_batch_item_failures`; processor must be idempotent (W-05). | done (FEAT-003): `retry_attempts=3`, `max_record_age=1h`, `bisect_batch_on_error`, `report_batch_item_failures` (the tumbling window response carries `state` and `batchItemFailures`, verified in the Lambda docs), SQS on-failure destination; asserted in tests |
| W-07 | Lambda baseline | No tracing, log retention (infinite), DLQ for async SNS invoke, reserved concurrency, `architecture`. Timeout 5 min and 512 MB for both. | [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) | Medium | `tracing=ACTIVE`, `log_retention`/explicit LogGroup 30 days, arm64, `reserved_concurrent_executions` for the processor, `dead_letter_queue` for the SNS-invoked handler, `POWERTOOLS`-style structured logs (JSON) without new dependencies. | done (FEAT-003): tracing ACTIVE, explicit 30 day log groups, arm64, SQS DLQ + 2 retries for the SNS invoked handler (and an SNS subscription DLQ), JSON logs, optional reserved concurrency by context (unset by default so new accounts with low limits can deploy) |
| W-08 | IAM on Lambdas | `social-messaging:*` and `bedrock-agentcore:InvokeAgentRuntime*` and `transcribe:*` on `*`. | [Security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html) | High | Scope to the runtime ARN, the WhatsApp phone number ARN pattern, and a bucket-prefix-restricted Transcribe flow. | done (FEAT-003): `social-messaging:*` on the phone-number ARN pattern, Transcribe on `transcription-job/whatsapp-audio-*`, invoke on the runtime ARN, DynamoDB limited to the actions each Lambda uses, secrets limited to the two secret ARNs; no wildcard on sensitive actions (asserted) |
| W-09 | Stack 01 and 02 synth requires AWS | `get_param.py` calls SSM at synth. | Baseline (section 2) | High | Replace with `StringParameter.value_for_string_parameter` or `value_from_lookup` guarded by context; remove `get_param.py` and the `boto3` import at synth. | done (FEAT-003): `StringParameter.value_for_string_parameter` (CloudFormation resolves SSM at deploy time); `cdk synth` runs with dummy credentials |
| W-10 | Media handling | Media is downloaded and base64'd into the Runtime payload; large inputs can exceed the practical request size. | [Runtime service contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html) | Medium | Keep limits aligned with the agent (A: validate decoded size); prefer S3 URIs for video (already done). | done (FEAT-003): processors `head_object` before inlining and answer with a size message above 1.5 MB (equal to the agent limit); receivers stream downloads with a 25 MB cap and a 20 s budget; video stays an S3 URI |
| W-11 | Session semantics | One continuous session per user (`wa-chat-{phone}` padded to 33 chars). Runtime idle timeout 15 min but memory session persists via AgentCore Memory. | [Memory organization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-organization.html) | Info | Keep. Add unit tests for the 33-char padding helper in both stacks. | done (FEAT-003): padding helpers moved to `message_utils.py` with tests in both stacks |

### 4.5 Data, storage, repo hygiene and docs

| ID | Area | Current state | Reference | Sev | Action | Status |
|---|---|---|---|---|---|---|
| D-01 | DynamoDB | `RemovalPolicy.DESTROY` everywhere, no PITR or deletion protection on `unified_users`, default AWS-owned key. | repo code | Medium | Keep DESTROY by default for a sample (documented), but add context flag `retain_data=true` that switches to RETAIN + PITR; enable PITR on users table by default. | done (FEAT-003): SSE (AWS managed key) on all tables, PITR always on the users table, `-c retain_data=true` switches tables and bucket to RETAIN with deletion protection; message buffer tables have no PITR (transient data, 48 h TTL) unless `retain_data` |
| D-02 | S3 buckets | Versioned, SSL enforced, block public: good. No lifecycle rule on media; `auto_delete_objects`. | repo code | Low | Add a 30-day expiration lifecycle for `images/`, `voice/`, `video/`, `documents/` prefixes (media is ephemeral by design). | done: stack 00 (FEAT-002); stacks 01 and 02 (FEAT-003): 30 day expiration per media prefix, 1 day for noncurrent versions, abort incomplete uploads, S3 managed encryption |
| D-03 | Committed binaries | 12 GIFs (up to 15 MB each, ~50 MB duplicated across root, 01 and 02) and `notebook/test_agentcore_deployed_executed.ipynb` (executed outputs) are tracked. | repo `git ls-files` | Medium | Remove the `_executed` notebook and ignore `*_executed.ipynb`; dedupe GIFs by keeping one copy and linking; do not rewrite history. | todo (FEAT-004) |
| D-04 | Docs staleness | `CLAUDE.md` is the only agent guide; no `AGENTS.md` at any level. READMEs mention the EOL model and wrong model ID example. | `ls`, repo docs | Medium | Create `AGENTS.md` at root, 00, 01, 02; make `CLAUDE.md` a pointer/symlink; refresh READMEs with TRK links and new versions. | todo (FEAT-004) |
| D-05 | Tests | None. | `find . -name "test*"` | High | Add pytest for: signature verification, 33-char id padding, message grouping, `_sanitize_document_name`, `_validate_media`, `build_multimodal_prompt`; CDK assertion tests per stack (runtime enum, arm64, throttling, no `*` on invoke). | partial: stack 00 (FEAT-002); stacks 01 (41 tests) and 02 (72 tests) done in FEAT-003; root checks FEAT-004 |
| D-06 | Notebooks | `test_agentcore_deployed.ipynb` and `test_multimodal_agent.ipynb` reference the old model and API usage. | repo notebooks | Low | Update model/version, validate with `nbformat`; strip outputs. | todo (FEAT-004) |

## 5. Documentation checked (live)

| Topic | URL |
|---|---|
| Strands session persistence (Snapshot manager) | https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/session-management/ |
| Strands AgentCore Memory session manager | https://strandsagents.com/latest/documentation/docs/community/session-managers/agentcore-memory/ |
| Strands Bedrock model provider | https://strandsagents.com/latest/documentation/docs/user-guide/concepts/model-providers/amazon-bedrock/ |
| Strands agent loop, limits, concurrency, idempotency | https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/agent-loop/ |
| Strands conversation management | https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/conversation-management/ |
| Strands hooks and plugins | https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/hooks/ |
| Strands traces (OTEL) | https://strandsagents.com/latest/documentation/docs/user-guide/observability-evaluation/traces/ |
| Strands deploy to AgentCore (Python) | https://strandsagents.com/latest/documentation/docs/user-guide/deploy/deploy_to_bedrock_agentcore/python/ |
| AgentCore direct code deployment | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html |
| AgentCore supported language runtimes | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html |
| AgentCore Runtime service contract | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html |
| AgentCore inbound/outbound auth | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html |
| AgentCore Runtime security best practices | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html |
| AgentCore Memory organization and namespaces | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-organization.html |
| AgentCore Memory strategies | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html and https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-strategies.html |
| AgentCore Observability | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html |
| Bedrock model lifecycle (new and legacy) | https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html , https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html |
| Bedrock model cards | https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html , https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html , https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html |
| Lambda runtimes | https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html |
| AWS End User Messaging Social | https://docs.aws.amazon.com/social-messaging/latest/userguide/what-is-service.html |
| Meta Graph API versions | https://developers.facebook.com/docs/graph-api/changelog/versions |
| Meta webhooks (verification, `X-Hub-Signature-256`) | https://developers.facebook.com/docs/graph-api/webhooks/getting-started |

Not retrievable as text (page returned no usable match or is JS-rendered): the Lambda DynamoDB event-source page (`with-ddb.html`) returned no "tumbling" match, so W-06 options were verified against the installed `aws-cdk-lib` instead. API Gateway throttling and WAF behavior and the Meta Instagram webhook page were not re-fetched; W-03 relies on well-known stable API Gateway features and should be confirmed by `cdk synth` assertions in FEAT-003.

## 6. Graph API version table (from the Meta changelog)

| Version | Released | Expires |
|---|---|---|
| v26.0 | 2026-07-29 | TBD |
| v25.0 | 2026-02-18 | 2028-07-29 |
| v24.0 | 2025-10-08 | 2028-02-18 |
| v21.0 | 2024-10-02 | 2027-01-21 |
| v20.0 | 2024-05-21 | 2026-09-24 (already expired) |

Chosen: `v25.0`.

## 7. Sequencing for the next features

1. FEAT-002 (stack 00): A-01, A-02, A-04, A-06, S-01..S-11, C-02..C-11, D-02 (00 part), tests, `AGENTS.md`.
2. FEAT-003 (stacks 01, 02): A-03, A-05, W-01..W-11, D-01, D-02, tests, `AGENTS.md`.
3. FEAT-004 (root, notebooks): D-03..D-06, README refresh with TRK links, merge `CLAUDE.md` into `AGENTS.md`, final verification.

## 8. FEAT-002 implementation notes (stack 00)

Verified against the installed packages (`bedrock-agentcore` 1.25.3, `strands-agents` 1.64.0, `aws-cdk-lib` 2.275.0) and live docs before use: `Agent(plugins=, conversation_manager=, trace_attributes=)`, `Agent.__call__(limits=, idempotency_token=)`, `AgentCoreMemoryConfig(batch_size=)`, `RetrievalConfig`, `BedrockModel(cache_config=CacheConfig, guardrail_*)`, `CfnMemory` `namespace_templates`, `EpisodicReflectionConfigurationInputProperty`, `CfnRuntime.request_header_configuration`, execution role statements from the AgentCore Runtime permissions page.

Deviations and findings:

* S-06: Strands' `idempotency_token` only de-duplicates overlapping calls on the same agent instance. Because the agent is now built per request, it has no effect on its own, so the agent also keeps a small in-process TTL cache keyed by `idempotency_key`. Callers (FEAT-003) should send the message ID as `idempotency_key`.
* S-03: namespaces use custom templates (`/facts/{actorId}/` and so on) instead of the documented `/strategy/{memoryStrategyId}/...` default, because strategy IDs are only known after creation. The strategy ID env vars were removed.
* C-06: the documented direct-deploy execution role keeps `Resource: "*"` for X-Ray write and `cloudwatch:PutMetricData` (namespace-conditioned) because they do not support resource-level permissions.
* `link_account` accepts unverified identifiers from chat (account takeover risk). Added `LINK_ALLOW_MERGE` (default true to keep the demo behavior) and documented it; a real verification step is out of scope. Also fixed a bug where the Instagram username scan used `Limit=1` (applied before the filter) and could miss matches.
* Memory execution role: built-in strategies use service-managed models, so the role's `InvokeModel` is scoped to the configured model only; no wildcard was needed.

## 9. FEAT-003 implementation notes (stacks 01 and 02)

Re-verified live before coding: Meta webhook signature header and retry behavior (36 hours, deduplicate on your side), Graph API versions (v25.0 expires 2028-07-29; v26.0 was released 2026-07-29 and is not adopted yet, override with the context key), Lambda runtimes (`python3.13` on Amazon Linux 2023, deprecation 2029-06-30; `python3.11` is AL2 and deprecates 2027-06-30), the tumbling window response shape (`state` plus `batchItemFailures`), partial batch response semantics, API Gateway throttling levels, and the End User Messaging Social `SendWhatsAppMessage` request (`metaApiVersion` required, phone number id or ARN accepted).

Findings and deviations:

* The messages table stored the WhatsApp and Instagram access tokens in every item (visible in the table and in the stream). Tokens are no longer stored; the processor reads them from Secrets Manager (5 minute cache) when it replies.
* The Instagram profile lookup and reply sent the access token in the URL or JSON body. They now use `Authorization: Bearer`, so the token cannot leak through logged URLs or error text.
* Meta's webhook can be mutually authenticated with mTLS, but REST API custom domains with mTLS are outside this sample. The HMAC check is the gate, as designed.
* SNS to Lambda delivery is authenticated by IAM (topic policy and Lambda resource policy), so SNS message signature verification (for HTTP endpoints) does not apply. The handler still verifies `EventSource` and `TopicArn`.
* Not verified: the supported `metaApiVersion` list of the Social API (the endpoints page was truncated). `v25.0` is the default; set `-c meta_api_version=` if the API rejects it in your Region.
* `ArnLike aws:SourceArn` on the SNS topic policy is opt in because it could not be confirmed that the service always sends that key.
* The webhook Lambda timeout is 28 seconds (API Gateway integration limit is 29 seconds). Media is still downloaded inside the request; if a download does not finish the request returns 500 and Meta retries, which is safe because of the conditional put.
* Message age limit for webhook messages is 15 minutes (was 5) so that short Meta retries are not dropped.
* Layer build: `layers/build_layer.sh` needs `uv` and network access at synth; use `-c layer_path=layers/common` to skip it (tests do).

