# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Three independent CDK stacks deployed in sequence, sharing configuration via SSM Parameter Store:

- **00-agent-agentcore** — Standalone AgentCore Runtime with a Strands multimodal agent + AgentCore Memory. Exports ARNs to SSM.
- **01-whatsapp-end-user-messaging** — WhatsApp via AWS End User Messaging Social: SNS -> receiver Lambda -> DynamoDB (Stream + tumbling window) -> processor Lambda -> AgentCore.
- **02-whatsapp-api-gateway** — WhatsApp via Meta Cloud API: API Gateway -> receiver Lambda -> DynamoDB (Stream + tumbling window) -> processor Lambda -> AgentCore.

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
cd 02-whatsapp-api-gateway/layers/common
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

- `actor_id` = `wa-user-{phone}` (padded to 33 chars) — identifies the USER. Keys long-term memory (facts, preferences) that persists across sessions.
- `session_id` = `wa-chat-{phone}` (padded to 33 chars) — identifies the CONVERSATION. Keys short-term memory (turns) that expires per TTL (default 3 days).

The architecture is multichannel: if multiple frontends generate the same `actor_id` for the same person, the agent remembers everything across all channels.

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

`get_param.py` (in stacks 01 and 02) reads SSM at CDK **synthesis** time using boto3. AWS credentials must be available when running `cdk synth/deploy`. Parameters exported by Stack 00: `/agentcore/agent_runtime_arn`, `/agentcore/s3_bucket_name`, `/agentcore/memory_id`, `/agentcore/runtime_role_arn`.

### Agent Runtime Requirements

- `multimodal_agent.py` must end with `app.run()` or the runtime fails to start within the 30s initialization window
- `requirements.txt` must include `bedrock-agentcore-starter-toolkit`, `requests`, and `twelvelabs`
- `create_deployment_package.sh` must NOT exclude any dependencies (strands needs watchdog, etc.)

### Stack 02 Secrets Manager

Three keys in the WhatsApp secrets (no `WHATS_PHONE_ID` — phone_id comes from the webhook payload):

- `WHATS_VERIFICATION_TOKEN` — webhook verify token (you define it, must match Meta config)
- `WHATS_TOKEN` — Meta Graph API access token
- `DISPLAY_PHONE_NUMBER` — business phone number for message filtering
