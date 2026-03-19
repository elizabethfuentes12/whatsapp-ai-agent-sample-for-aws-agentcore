# WhatsApp AI Agent Sample for Amazon Bedrock AgentCore

Two WhatsApp integration patterns using a shared **multimodal AI agent** deployed on [**Amazon Bedrock AgentCore Runtime**](https://aws.amazon.com/bedrock/agentcore/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) with [**Amazon Bedrock AgentCore Memory**](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el). The agent processes text, images, audio, video, and documents — storing text-based understanding in memory since AgentCore Memory only accepts text content.

> This demo uses [Strands Agents](https://github.com/strands-agents/sdk-python) with Amazon Bedrock AgentCore. Similar patterns can be applied with LangGraph, AutoGen, or other agent frameworks.

> **Note**: This guide assumes familiarity with [AWS Cloud Development Kit (AWS CDK)](https://aws.amazon.com/cdk/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), [AWS Lambda](https://aws.amazon.com/lambda/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), and WhatsApp Business API concepts.

## Projects

| Project | Description | Stack |
|---------|-------------|-------|
| [00-agent-agentcore](./00-agent-agentcore/) | Standalone Amazon Bedrock AgentCore Runtime with multimodal Strands agent + AgentCore Memory. Exports ARN to SSM. | ![Python](https://img.shields.io/badge/Python-3.11-blue) ![AgentCore](https://img.shields.io/badge/AWS-AgentCore-orange) ![Strands](https://img.shields.io/badge/Strands-Agents-green) |
| [01-whatsapp-end-user-messaging](./01-whatsapp-end-user-messaging/) | WhatsApp via AWS End User Messaging Social (Amazon SNS -> AWS Lambda -> AgentCore) | ![Python](https://img.shields.io/badge/Python-3.11-blue) ![CDK](https://img.shields.io/badge/AWS-CDK-orange) ![SNS](https://img.shields.io/badge/AWS-SNS-yellow) |
| [02-whatsapp-api-gateway](./02-whatsapp-api-gateway/) | WhatsApp via Meta Cloud API (Amazon API Gateway -> Amazon DynamoDB Stream -> AWS Lambda pipeline -> AgentCore) | ![Python](https://img.shields.io/badge/Python-3.12-blue) ![CDK](https://img.shields.io/badge/AWS-CDK-orange) ![APIGW](https://img.shields.io/badge/AWS-API_Gateway-purple) |
| [notebook](./notebook/) | Jupyter notebook to test the deployed agent | ![Jupyter](https://img.shields.io/badge/Jupyter-Notebook-orange) |

## Architecture

```
                                    +---------------------------+
                                    |  00-agent-agentcore       |
                                    |  (AgentCore Runtime +     |
                                    |   AgentCore Memory)       |
                                    |  Exports ARN via SSM      |
                                    +---------------------------+
                                           ^            ^
                                           |            |
                        +------------------+            +------------------+
                        |                                                  |
              +---------+-----------+                        +-------------+---------+
              | 01-end-user-msg     |                        | 02-api-gateway        |
              | AWS End User Msg    |                        | Meta WhatsApp Cloud   |
              | Amazon SNS -> Lambda|                        | API GW -> Lambda      |
              +---------------------+                        +------------------------+
```

## How Multimedia Memory Works

[Amazon Bedrock AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) only stores **text**. When a user sends multimedia via WhatsApp:

1. **Image** — The agent analyzes the image using Claude's vision (inline content blocks), creates a detailed text description, and that description is stored in memory
2. **Audio** — [Amazon Transcribe](https://aws.amazon.com/transcribe/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) converts speech to text in the Lambda, then the transcript is sent to the agent as a text prompt
3. **Video** — The agent uses a `video_analysis` tool powered by **TwelveLabs Pegasus** (`twelvelabs.pegasus-1-2-v1:0`) via [Amazon Bedrock](https://aws.amazon.com/bedrock/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el). This provides rich visual + audio understanding (scenes, actions, on-screen text, spoken words). The text analysis is stored in memory
4. **Document** (PDF, DOCX) — The agent reads the document via inline content blocks and summarizes the content, storing the summary in memory

Media files are organized in S3 by type: `images/`, `voice/`, `video/`, `documents/`.

The agent can then answer follow-up questions about any previously shared multimedia using the stored text understanding.

### Supported Media Formats and Limits

| Media | Formats | Limits |
|-------|---------|--------|
| **Image** | JPEG, PNG, GIF, WebP | Max 5 MB per image. Max resolution 8000x8000 px (optimal under 1568 px on longest edge) |
| **Document** | PDF, CSV, DOC, DOCX, XLS, XLSX, HTML, TXT, MD | Max ~1.5 MB via WhatsApp. PDFs up to 600 pages |
| **Audio** | OGG, MP3, AAC, M4A, WAV, AMR | Any format WhatsApp supports. Automatically transcribed via [Amazon Transcribe](https://aws.amazon.com/transcribe/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) |
| **Video** | MP4, MOV, MKV, WebM, FLV, MPEG, 3GP | Max 2 GB / 1 hour. Minimum ~4 seconds. Standard codec required (H.264/H.265). Analyzed via [TwelveLabs Pegasus](https://aws.amazon.com/marketplace/pp/prodview-mf4e5dbnkqvck?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) |

## Amazon Bedrock AgentCore Runtime: Session Lifecycle

Each [Amazon Bedrock AgentCore Runtime session](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) runs in an isolated **microVM** with dedicated compute, memory, and filesystem:

| Parameter | Value |
|-----------|-------|
| **Maximum session duration** | 8 hours |
| **Idle timeout** | 15 minutes of inactivity |
| **Isolation** | Dedicated microVM per session |

Sessions progress through three states:

1. **Active** — Processing requests, executing commands, or running background tasks
2. **Idle** — Completed processing but remains available for future invocations
3. **Terminated** — Execution environment is shut down and memory is sanitized

A session terminates when it reaches 8 hours, stays idle for 15 minutes, or fails a health check. After termination, the microVM is destroyed. The next invocation starts a fresh container.

## Amazon Bedrock AgentCore Memory: How Sessions and Identity Work

[Amazon Bedrock AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) has two layers that work together:

| Layer | Scope | What it stores | Lifetime |
|-------|-------|----------------|----------|
| **Short-term** | Per session | Conversation turns (events) | Expires per configured TTL (default 3 days) |
| **Long-term** | Per actor (cross-session) | Extracted facts, preferences, summaries | Persists indefinitely |

Two IDs drive this separation:

| ID | Format | Identifies | Used for |
|----|--------|-----------|----------|
| `actor_id` | `wa-user-{phone}` | The **user** | Long-term memory — facts and preferences that persist across all sessions |
| `session_id` | `wa-chat-{phone}` | The **conversation** | Short-term memory — conversation turns that expire per the TTL |

**Why they must be different**: The SDK enforces that `session_id != actor_id`. They serve distinct purposes — the actor is *who* the user is, the session is *which conversation* they are in.

**Why no business phone in the IDs**: A user is the same person regardless of which business number they contact. Their long-term memory (preferences, facts) follows them across channels.

**How it flows**:

```
Lambda generates:
  actor_id  = "wa-user-573001234567"   (padded to 33 chars)
  session_id = "wa-chat-573001234567"   (padded to 33 chars)

Calls invoke_agent_runtime:
  runtimeSessionId = session_id    -> context.session_id in the agent
  runtimeUserId    = actor_id      -> also sent in payload for reliability

Agent configures AgentCoreMemoryConfig:
  actor_id  -> long-term memory namespaces: /users/{actor_id}/facts, /users/{actor_id}/preferences
  session_id -> short-term memory: conversation turns stored as events
```

When the user sends an image, the agent creates a detailed text description. That description enters short-term memory as a conversation event, and AgentCore automatically extracts facts into long-term memory. Even after the session events expire, the extracted facts remain — so the agent can answer questions about that image months later.

## Deployment Sequence

Deploy stacks in order — each reads configuration from [AWS Systems Manager Parameter Store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el):

```bash
# Step 0: Deploy the shared AgentCore Runtime
cd 00-agent-agentcore
python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
bash create_deployment_package.sh
cdk deploy

# Step 1a: Deploy WhatsApp via End User Messaging (Option A)
cd ../01-whatsapp-end-user-messaging
python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
cdk deploy

# Step 1b: Deploy WhatsApp via API Gateway (Option B)
cd ../02-whatsapp-api-gateway
python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
cdk deploy
```

## Prerequisites

- [AWS CLI](https://aws.amazon.com/cli/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) configured with appropriate credentials
- [AWS CDK](https://aws.amazon.com/cdk/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) v2 installed (`npm install -g aws-cdk`)
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- For Option A (01): [AWS End User Messaging Social](https://aws.amazon.com/end-user-messaging/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) configured with a WhatsApp Business number
- For Option B (02): Meta Developer account with WhatsApp Business API access
- [TwelveLabs Pegasus](https://aws.amazon.com/marketplace/pp/prodview-mf4e5dbnkqvck?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) model enabled in Amazon Bedrock console (`twelvelabs.pegasus-1-2-v1:0`) for video analysis

## Known Limitations

- **Payload size limit**: The Amazon Bedrock AgentCore Runtime `invoke_agent_runtime` API has a payload size limit. Base64-encoded images over ~1.5 MB may cause **500 errors** from the runtime. For large images, consider resizing or compressing before sending, or uploading to S3 and passing the URI instead.

- **Runtime session lifecycle**: Each session runs in an [isolated microVM](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el). Sessions stay alive for up to **8 hours** and terminate after **15 minutes** of inactivity. After redeploying agent code or IAM role changes, wait for the idle timeout (15 min) or start a new session to pick up the changes.

- **Runtime initialization timeout (30s)**: The deployment package must include `bedrock-agentcore-starter-toolkit` in `requirements.txt`, and `multimodal_agent.py` must end with `app.run()`. Without these, the runtime fails to start within the 30-second window.

- **Content blocks ordering**: When sending multimodal content blocks to the agent, text must be the **first** content block. `AgentCoreMemorySessionManager` reads `content[0]["text"]` and will fail with a `KeyError` if the first block is an image or document.

## Troubleshooting

- **SSM parameter not found**: Ensure you deployed `00-agent-agentcore` first and in the same AWS account/region
- **AgentCore Memory errors**: Verify the Memory ID is correctly set in the AgentCore Runtime environment. See [AgentCore Memory docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)
- **WhatsApp webhook failures**: Check [CloudWatch Logs](https://aws.amazon.com/cloudwatch/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) for the Lambda function handling webhooks
- **Media processing timeout**: Lambda timeout is set to 5 minutes; large files may need more time
- **Video analysis S3 errors**: The AgentCore runtime role needs `s3:GetObject` on the media bucket. Stack 01 grants this automatically using the runtime role ARN exported via SSM by Stack 00

---

## Contributing

Contributions are welcome! See [CONTRIBUTING](CONTRIBUTING.md) for more information.

---

## Security

If you discover a potential security issue in this project, notify AWS/Amazon Security via the [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file for details.
