# 00 - Amazon Bedrock AgentCore Runtime + Multimodal Agent

Standalone [**Amazon Bedrock AgentCore Runtime**](https://aws.amazon.com/bedrock/agentcore/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) with a [**Strands Agents**](https://github.com/strands-agents/sdk-python) multimodal agent and [**Amazon Bedrock AgentCore Memory**](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el). This is the foundational stack — the WhatsApp integration stacks (01 and 02) depend on it.

The agent processes text, images, documents, audio transcripts, and videos. Since Amazon Bedrock AgentCore Memory only stores text, all multimedia is first understood by the agent and the text understanding is stored in memory.

## Architecture

```
                    +----------------------------------+
                    |        AgentCore Runtime          |
                    |  ┌─────────────────────────────┐  |
                    |  │   Strands Multimodal Agent   │  |
                    |  │   - Claude vision (images)   │  |
                    |  │   - Claude docs (PDF/DOCX)   │  |
                    |  │   - video_analysis tool       │  |
                    |  │     (TwelveLabs Pegasus)      │  |
                    |  └─────────────────────────────┘  |
                    |  ┌─────────────────────────────┐  |
                    |  │     AgentCore Memory         │  |
                    |  │   - Short-term (per session) │  |
                    |  │   - Long-term (per user)     │  |
                    |  └─────────────────────────────┘  |
                    +----------------------------------+
                              ↑           ↑
                         01-stack      02-stack
                       (via SSM)     (via SSM)
```

## Key Files

| File | Purpose |
|------|---------|
| `agent_files/multimodal_agent.py` | Strands Agent — handles text, image, document, audio, video |
| `agent_files/video_analysis_tool.py` | Strands tool calling [TwelveLabs Pegasus](https://aws.amazon.com/marketplace/pp/prodview-mf4e5dbnkqvck?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) via Bedrock |
| `agent_files/requirements.txt` | Runtime dependencies (strands-agents, bedrock-agentcore) |
| `create_deployment_package.sh` | Builds ARM64-optimized ZIP for AgentCore Runtime |
| `agentcore/agentcore_deployment.py` | CDK construct for [CfnRuntime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtimes.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) |
| `agentcore/agentcore_memory.py` | CDK construct for [CfnMemory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) (semantic + user preference strategies) |
| `agentcore/agentcore_role.py` | IAM role with Bedrock, CloudWatch, XRay permissions |

## Multimedia Processing

| Media Type | Formats | Limits | How It Works |
|------------|---------|--------|-------------|
| **Image** | JPEG, PNG, GIF, WebP | Max 5 MB, max 8000x8000 px | Claude vision via inline content blocks |
| **Document** | PDF, CSV, DOC, DOCX, XLS, XLSX, HTML, TXT, MD | Max ~1.5 MB, PDFs up to 600 pages | Claude reads via inline content blocks |
| **Video** | MP4, MOV, MKV, WebM, FLV, MPEG, 3GP | Max 2 GB / 1 hour, min ~4s, H.264/H.265 | `video_analysis` tool via [TwelveLabs Pegasus](https://aws.amazon.com/marketplace/pp/prodview-mf4e5dbnkqvck?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) on [Amazon Bedrock](https://aws.amazon.com/bedrock/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) |
| **Audio** | OGG, MP3, AAC, M4A, WAV, AMR | Any WhatsApp format | [Amazon Transcribe](https://aws.amazon.com/transcribe/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) → text prompt |

## Runtime Session Lifecycle

Each [Amazon Bedrock AgentCore Runtime session](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) runs in an isolated microVM:

| Parameter | Value |
|-----------|-------|
| **Maximum session duration** | 8 hours |
| **Idle timeout** | 15 minutes of inactivity |
| **Isolation** | Dedicated microVM per session |

After code or IAM changes, wait 15 minutes for idle sessions to terminate, or use a new `session_id` to start a fresh container.

## SSM Parameters Exported

| Parameter | Value |
|-----------|-------|
| `/agentcore/agent_runtime_arn` | AgentCore Runtime ARN |
| `/agentcore/s3_bucket_name` | S3 bucket for media/code |
| `/agentcore/memory_id` | AgentCore Memory ID |
| `/agentcore/runtime_role_arn` | Runtime execution role ARN (used by Stack 01 to grant S3 read access) |

## Deploy

```bash
cd 00-agent-agentcore
python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
bash create_deployment_package.sh   # builds ARM64 ZIP in agent_files/
cdk deploy
```

## Environment Variables (Runtime)

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_REGION` | `us-east-1` | AWS region |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Claude model for the agent |
| `BEDROCK_AGENTCORE_MEMORY_ID` | Set by stack | Memory resource ID |
| `S3_BUCKET` | Set by stack | S3 bucket name |

## CDK Resources

- **S3 Bucket** — versioned, auto-delete on stack removal
- **[CfnMemory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)** — semantic + user preference strategies, 3-day TTL
- **[CfnRuntime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtimes.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)** — Python 3.11, PUBLIC network mode, ARM64
- **CfnRuntimeEndpoint** — `WhatsAppAgentEndpoint`
- **IAM Role** — Bedrock, CloudWatch, XRay, S3 access, Marketplace subscriptions
- **[SSM Parameters](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)** — 4 parameters for cross-stack sharing (runtime ARN, bucket, memory ID, role ARN)
