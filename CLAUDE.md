# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Three independent CDK stacks deployed in sequence, sharing configuration via SSM Parameter Store:

- **00-agent-agentcore** — Standalone AgentCore Runtime with a Strands multimodal agent + AgentCore Memory. Exports ARN to SSM `/agentcore/agent_runtime_arn`.
- **01-whatsapp-end-user-messaging** — WhatsApp via AWS End User Messaging Social: SNS -> single Lambda -> AgentCore. Reads agent ARN from SSM.
- **02-whatsapp-api-gateway** — WhatsApp via Meta Cloud API: API Gateway -> DynamoDB Stream -> Lambda pipeline (6 functions) -> AgentCore. Reads agent ARN from SSM.

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

After deploying Stack 00, update the TwelveLabs API key in Secrets Manager:

```bash
aws secretsmanager put-secret-value \
  --secret-id <TwelveLabsSecretArn from stack output> \
  --secret-string '{"TL_API_KEY":"your-actual-key"}'
```

## Key Design Decisions

### AgentCore Memory IDs

Two IDs drive memory — they must be different strings (enforced by the SDK):

- `actor_id` = `wa-user-{phone}` (padded to 33 chars) — identifies the USER. Keys long-term memory (facts, preferences) that persists across sessions.
- `session_id` = `wa-chat-{phone}` (padded to 33 chars) — identifies the CONVERSATION. Keys short-term memory (turns) that expires per TTL (default 3 days, minimum allowed by the API).

The `actor_id` is sent both in the payload and as `runtimeUserId` for reliability. The agent reads `session_id` from `context.session_id` and `actor_id` from the payload with fallback chain.

### Multimedia Processing

AgentCore Memory only stores text. All multimedia is converted to text before entering memory:

- **Image**: Claude vision (inline content blocks) — text block MUST be first in content array
- **Audio**: Amazon Transcribe -> transcript sent as text prompt to agent (no media block)
- **Video**: TwelveLabs API direct (upload, query, list_videos actions). API key stored in Secrets Manager, read at runtime via `TL_SECRET_ARN` env var. Videos are uploaded to TwelveLabs for indexing; agent stores `[VIDEO: id={video_id} | desc="{description}"]` tags in its response so the video_id persists in memory for follow-up queries.
- **Document**: Claude reads PDF/DOCX inline via document content blocks — filename must be sanitized (alphanumeric, spaces, hyphens, parens, brackets only)

### Video Analysis (TwelveLabs Direct)

The `video_analysis` Strands tool calls TwelveLabs API (`api.twelvelabs.io/v1.3`) directly — NOT via Bedrock Marketplace. Three actions:

- `upload`: S3 URI -> pre-signed URL -> TwelveLabs indexing. Returns video_id + metadata (title, topics, hashtags).
- `query`: video_id + prompt -> TwelveLabs analyze API. For follow-up questions about previously uploaded videos.
- `list_videos`: Lists all indexed videos across all TwelveLabs indexes.

The API key is stored in **Secrets Manager** (created by CDK with placeholder value). The tool reads it once at startup and caches it. The runtime role has `secretsmanager:GetSecretValue` permission via `tl_secret.grant_read()`.

### Content Block Ordering (Critical)

When sending multimodal content blocks to the agent, **text must be the first content block**. `AgentCoreMemorySessionManager` reads `content[0]["text"]` for memory retrieval. If the first block is an image or document, it crashes with `KeyError: 'text'` and the invalid content pollutes both short-term and long-term memory permanently.

### Input Validation (Critical)

All media must be validated BEFORE reaching the agent to prevent memory contamination:

- Image formats: jpeg, png, gif, webp
- Document formats: pdf, csv, doc, docx, xls, xlsx, html, txt, md
- Document filenames: sanitized to remove special characters
- Max payload size: ~1.5 MB for base64 content
- Video: requires valid S3 URI

If invalid content enters memory, it cannot be deleted per-record — the entire AgentCore Memory resource must be recreated.

### Runtime Container Caching

AgentCore runs each session in an isolated microVM. Sessions stay active for up to **8 hours** and terminate after **15 minutes** of inactivity. After redeploying agent code or IAM role changes, wait for the idle timeout (15 min) or use a new `session_id` to start a fresh container. Changing session_id prefix does NOT help for long-term memory issues because long-term memory is keyed by `actor_id`.

### SSM Parameter Sharing

`get_param.py` (in both 01 and 02) reads SSM at CDK **synthesis** time using boto3 directly. This means AWS credentials must be available when running `cdk synth/deploy` for stacks 01 and 02. The region is read from `AWS_REGION` or `CDK_DEFAULT_REGION` environment variables. SSM parameters exported by Stack 00: `/agentcore/agent_runtime_arn`, `/agentcore/s3_bucket_name`, `/agentcore/memory_id`, `/agentcore/runtime_role_arn`. Stack 01 uses the runtime role ARN to grant S3 read access on its media bucket (needed for video pre-signed URLs).

### Agent Prompt

The system prompt is intentionally compact for token efficiency. It instructs the agent to: respond in the user's language, be concise, preserve key details in text responses (since memory is text-only), and use `[VIDEO: id=X | desc="Y"]` tags for video tracking. Defined in `00-agent-agentcore/agent_files/multimodal_agent.py`.

### Agent Runtime Requirements

- `multimodal_agent.py` must end with `app.run()` or the runtime fails to start within the 30s initialization window
- `requirements.txt` must include `bedrock-agentcore-starter-toolkit` and `requests`
- `create_deployment_package.sh` must NOT exclude any dependencies (strands needs watchdog, etc.)

## Current State

- **Stack 00 (AgentCore)**: AgentCore Runtime + Memory deployed with lifecycle configuration. TwelveLabs API key in Secrets Manager.
- **Stack 01 (End User Messaging)**: Lambda processes text, image, audio, video, document. SNS topic connected to End User Messaging Social.
- **Stack 02 (API Gateway)**: 6-Lambda pipeline deployed with API Gateway webhook.
