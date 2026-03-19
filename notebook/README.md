# Notebook — Test the Deployed Agent

Jupyter notebooks to test the **deployed AgentCore Runtime** directly, validating all multimedia types and memory before connecting WhatsApp.

## Notebooks

| Notebook | Purpose |
|----------|---------|
| `test_agentcore_deployed.ipynb` | End-to-end test of the deployed runtime — text, image, audio, video, document, memory |
| `test_multimodal_agent.ipynb` | Local test of the agent logic (imports agent code directly) |

## Test Files

| File | Size | Used For |
|------|------|----------|
| `imagen2.png` | ~2.4 MB | Image processing test |
| `Runtime.mp4` | ~220 MB | Video analysis test (TwelveLabs Pegasus) |

## Setup

The notebook requires the custom `bedrock-agentcore` botocore service model (not yet in the public boto3). It loads this from the deployment package:

```python
import botocore.loaders, botocore.session

CUSTOM_DATA_PATH = "../00-agent-agentcore/agent_files/deployment_package/botocore/data"

def agentcore_boto3_session(region):
    bcore = botocore.session.get_session()
    loader = botocore.loaders.Loader(extra_search_paths=[CUSTOM_DATA_PATH])
    bcore.register_component("data_loader", loader)
    return boto3.Session(botocore_session=bcore, region_name=region)
```

## Prerequisites

- Stack `00-agent-agentcore` deployed
- AWS credentials configured
- `pip install boto3`

## What It Tests

1. **Text** — Basic prompt and response
2. **Memory recall** — Agent remembers name and context from prior turns
3. **Image** — Base64-encoded PNG sent as content block
4. **Audio transcript** — Simulated transcription sent as text
5. **Video** — Uploaded to S3, analyzed with TwelveLabs Pegasus via `video_analysis` tool
6. **Document** — Base64-encoded PDF/DOCX sent as content block
7. **Cross-turn memory** — Agent summarizes the full conversation
8. **Memory records** — Queries AgentCore Memory API to inspect stored data

## Payload Formats

```json
// Text
{"prompt": "Hello", "actor_id": "wa-user-..."}

// Image
{"prompt": "Describe this", "actor_id": "...", "media": {"type": "image", "format": "png", "data": "<base64>"}}

// Document
{"prompt": "Summarize", "actor_id": "...", "media": {"type": "document", "format": "docx", "data": "<base64>", "name": "file"}}

// Video
{"prompt": "Analyze", "actor_id": "...", "media": {"type": "video", "s3_uri": "s3://bucket/key.mp4"}}

// Audio (transcript as text — no media block)
{"prompt": "Audio transcription: \"text here\"", "actor_id": "..."}
```
