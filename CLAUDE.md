# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Three independent CDK stacks deployed in sequence, sharing configuration via SSM Parameter Store:

- **00-agent-agentcore** — Standalone AgentCore Runtime with a Strands multimodal agent + AgentCore Memory. Exports ARNs to SSM.
- **01-whatsapp-end-user-messaging** — WhatsApp via AWS End User Messaging Social: SNS -> receiver Lambda -> DynamoDB (Stream + tumbling window) -> processor Lambda -> AgentCore.
- **02-multichannel-api-gateway** — WhatsApp + Instagram DM via Meta Cloud API: API Gateway -> receiver Lambda (dual-channel detection) -> DynamoDB (Stream + tumbling window) -> processor Lambda -> AgentCore. Single webhook serves both platforms.

Each stack has its own `app.py`, `cdk.json`, and `requirements.txt`. They are independent CDK apps, not a single multi-stack app.

## Build & Deploy Commands

Each stack is deployed from its own directory:

```bash
cd 00-agent-agentcore  # or 01-... or 02-...
python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
cdk deploy
```

For the agent stack specifically, build the deployment package first:

```bash
cd 00-agent-agentcore
bash create_deployment_package.sh  # builds ARM64 ZIP in agent_files/
cdk deploy
```

For Stack 02, install Lambda layer dependencies before deploy:

```bash
cd 02-multichannel-api-gateway/layers/common
pip install requests -t python/
cd ../..
cdk deploy
```

After deploying Stack 00, update the TwelveLabs API key in Secrets Manager:

```bash
aws secretsmanager put-secret-value \
  --secret-id <TwelveLabsSecretArn from stack output> \
  --secret-string '{"TL_API_KEY":"your-actual-key"}'
```

## Key Design Decisions

### Message Buffering (Tumbling Window)

Both Stack 01 and 02 use DynamoDB Streams with a tumbling window (20 seconds) to aggregate rapid-fire WhatsApp messages into a single agent invocation. Based on [sample-whatsapp-end-user-messaging-connect-chat](https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat).

- DynamoDB table PK=`from_phone`, SK=`id` ensures same-user messages land in the same shard
- `tumbling_window` + `max_batching_window` on the Lambda event source mapping (configurable via `buffer_seconds` in CDK)
- Processor Lambda deserializes DDB stream records, groups by sender, concatenates texts with `\n`, keeps last media

### Configurable Models

Both the LLM and video analysis model are configurable via environment variables set before `cdk deploy`:

- `MODEL_ID` — Claude model (default: `us.anthropic.claude-sonnet-4-20250514-v1:0`)
- `TL_MODEL_NAME` — TwelveLabs model (default: `pegasus1.2`)

### AgentCore Memory IDs

Two IDs drive memory — they must be different strings (enforced by the SDK):

- **actor_id**: Canonical `u-user-{uuid}` from the unified users table (padded to 33 chars). Same across all channels for linked users.
- **session_id**: Channel-specific — `wa-chat-{phone}` or `ig-chat-{sender_id}` (padded to 33 chars). Keeps conversation turns separate per channel.

Stack 01 (SNS) uses legacy format `wa-user-{phone}` as actor_id (no unified users table).

### Unified User Identity (Stack 02)

The `unified_users` DynamoDB table maps channel-specific IDs to a canonical `user_id`:
- GSI `wa-phone-index` on `wa_phone` for WhatsApp lookups
- GSI `ig-id-index` on `ig_id` for Instagram lookups
- Table name exported to SSM: `/agentcore/unified_users_table_name`
- AgentCore runtime role has read/write access

The agent has a `link_account` tool that merges two user records when a user provides their other channel identity. The tool reads the table name from SSM at runtime (`ssm:GetParameter` on `/agentcore/*`). This permission is granted in `agentcore_role.py` (Stack 00) because the table is created by Stack 02 after Stack 00 deploys — SSM bridges the dependency.

**Important**: When `link_account` is called from WhatsApp with an IG username, it saves `ig_username` but cannot resolve the numeric `ig_id` (only known when IG sends a webhook). The processor handles this with a fallback: if lookup by `ig_id` GSI fails, it scans by `ig_username`, finds the linked user, and backfills the `ig_id` for instant future lookups.

### Memory Retrieval Config

- Namespace format: `/strategies/{memoryStrategyId}/actors/{actorId}/` (NOT `/users/{actorId}/facts`)
- Strategy IDs passed as env vars: `FACTS_STRATEGY_ID`, `PREFERENCES_STRATEGY_ID` (set at `cdk deploy` time)
- `top_k: 20` for facts, `top_k: 10` for preferences, `relevance_score: 0.3` for both
- The agent prompt includes explicit "Fact:" lines in responses to improve long-term memory extraction quality (AgentCore summarizes aggressively and drops structured data like IDs)

### Agent Prompt Tags

The processor prepends context tags to every prompt sent to AgentCore:
- `[Channel: whatsapp|instagram]` — current channel
- `[UserID: user-xxx]` — canonical user ID (only in Stack 02)
- `[User: Name]` — display name from WhatsApp contact or Instagram profile

The agent only offers cross-channel linking when `[UserID:]` is present.

### Multimedia Processing

AgentCore Memory only stores text. All multimedia is converted to text before entering memory:

- **Image**: Claude vision (inline content blocks) — text block MUST be first in content array
- **Audio**: Amazon Transcribe -> transcript sent as text prompt to agent (no media block)
- **Video**: TwelveLabs API direct (`api.twelvelabs.io/v1.3`), NOT via Bedrock Marketplace. Agent stores `[VIDEO: id={video_id} | desc="{description}"]` tags for follow-up queries. API key in Secrets Manager (`TL_SECRET_ARN` env var).
- **Document**: Claude reads PDF/DOCX inline — filename must be sanitized (alphanumeric, spaces, hyphens, parens, brackets only)

### Content Block Ordering (Critical)

When sending multimodal content blocks to the agent, **text must be the first content block**. `AgentCoreMemorySessionManager` reads `content[0]["text"]` for memory retrieval. If the first block is an image or document, it crashes with `KeyError: 'text'` and the invalid content pollutes memory permanently.

### Input Validation (Critical)

All media must be validated BEFORE reaching the agent to prevent memory contamination. If invalid content enters memory, it cannot be deleted per-record — the entire AgentCore Memory resource must be recreated.

### Agent Security Prompt

The system prompt includes security rules: never reveal S3 bucket names, ARNs, error stack traces, or internal details to users. If a tool fails, the agent says "I had a technical issue" without sharing the error. The prompt also handles personalization via `[User: Name]` tags extracted from WhatsApp contact profiles.

### Runtime Container Caching

AgentCore runs each session in an isolated microVM. Sessions stay active for up to **8 hours** and terminate after **15 minutes** of inactivity. After redeploying agent code, wait for the idle timeout or use a new `session_id`.

### SSM Parameter Sharing

`get_param.py` (in stacks 01 and 02) reads SSM at CDK **synthesis** time using boto3. AWS credentials must be available when running `cdk synth/deploy`. Parameters exported by Stack 00: `/agentcore/agent_runtime_arn`, `/agentcore/s3_bucket_name`, `/agentcore/memory_id`, `/agentcore/runtime_role_arn`. Stack 02 exports: `/agentcore/unified_users_table_name`.

### Agent Runtime Requirements

- `multimodal_agent.py` must end with `app.run()` or the runtime fails to start within the 30s initialization window
- `requirements.txt` must include `bedrock-agentcore-starter-toolkit`, `requests`, and `twelvelabs`
- `create_deployment_package.sh` must NOT exclude any dependencies (strands needs watchdog, etc.)

### Stack 02 Secrets Manager

**WhatsApp secret** (no `WHATS_PHONE_ID` — phone_id comes from the webhook payload):

- `WHATS_VERIFICATION_TOKEN` — webhook verify token (you define it, must match Meta config)
- `WHATS_TOKEN` — Meta Graph API access token
- `DISPLAY_PHONE_NUMBER` — business phone number for message filtering

**Instagram secret** (separate from WhatsApp — different token):

- `IG_TOKEN` — Instagram API access token (generated from Meta App Dashboard > Instagram > API setup with Instagram business login)
- `IG_ACCOUNT_ID` — Instagram Business Account ID (used to filter own messages / echo)
- `IG_VERIFICATION_TOKEN` — webhook verify token for Instagram (you define it)

### AgentCore Invocation Retry (Stack 02)

The processor Lambda retries `InvokeAgentRuntime` with exponential backoff (3 attempts, base 2s) on:
- `RuntimeClientError` (424) — runtime microVM unavailable or crashed
- `InternalServerException` (500) — temporary, resolves with retries per AWS docs
- `ThrottlingException` (429) — rate limit exceeded

This handles cold starts after session resume (new microVM provisioning) and transient runtime errors.
References: [InvokeAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html), [Runtime Sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html).

### Stack 02 Dual-Channel Webhook

The receiver Lambda detects the channel from `body["object"]`:
- `whatsapp_business_account` -> WhatsApp processing (parses `entry[].changes[].value.messages[]`)
- `instagram` -> Instagram processing (parses `entry[].messaging[]`)

Instagram messages use `from_phone = ig-{sender_id}` as DDB partition key to avoid collision with WhatsApp phone numbers. The processor reads the `channel` field from each DDB record to dispatch replies via the correct API.
