# Notebook — Test the Deployed Amazon Bedrock AgentCore Agent

Jupyter notebooks to test the deployed [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtimes.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) directly, validating all multimedia types and memory persistence before connecting a WhatsApp frontend. This is the recommended way to verify your agent works correctly after deploying Stack 00.

> **Prerequisite**: Stack `00-agent-agentcore` **must be deployed first**. The notebook reads configuration from [AWS Systems Manager Parameter Store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) parameters exported by the stack. You also need to run `bash create_deployment_package.sh` in `00-agent-agentcore/` to generate the custom botocore service model. See the [Stack 00 README](../00-agent-agentcore/README.md) for deployment instructions.

## What notebooks are available?

| Notebook | Purpose |
|----------|---------|
| `test_agentcore_deployed.ipynb` | End-to-end test of the deployed runtime — text, image, audio, video, document, and memory recall |
| `test_agentcore_deployed_executed.ipynb` | Same notebook with pre-executed outputs for reference |
| `test_multimodal_agent.ipynb` | Local test of the agent logic (imports agent code directly, does not require deployment) |

## What test files are included?

| File | Size | Used for |
|------|------|----------|
| `imagen2.png` | ~2.4 MB | Image processing test (Claude vision) |
| `Runtime.mp4` | ~220 MB | Video analysis test ([TwelveLabs](https://www.twelvelabs.io/) Pegasus) |

## What does it test?

The `test_agentcore_deployed.ipynb` notebook validates the full agent pipeline:

1. **Text message** — Basic prompt and response through the deployed endpoint
2. **Memory recall** — Agent remembers name and context from prior turns (short-term memory)
3. **Image processing** — Base64-encoded PNG sent as inline content block (Claude vision)
4. **Audio transcript** — Simulated transcription sent as text prompt
5. **Video analysis** — Video uploaded to Amazon S3, analyzed with [TwelveLabs Pegasus](https://docs.twelvelabs.io/docs/concepts/models) via the `video_analysis` tool
6. **Document processing** — Base64-encoded PDF/DOCX sent as inline content block
7. **Cross-turn memory** — Agent summarizes the full conversation demonstrating context retention
8. **Memory records inspection** — Queries the [AgentCore Memory API](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) to inspect stored facts and preferences

## What are the prerequisites?

- [Stack 00 (`00-agent-agentcore`)](../00-agent-agentcore/README.md) deployed successfully
- Deployment package built (`bash create_deployment_package.sh` in `00-agent-agentcore/`)
- [AWS CLI](https://aws.amazon.com/cli/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) configured with credentials that can invoke AgentCore Runtime
- Python 3.11 or later with `boto3` installed
- [TwelveLabs API key](https://dashboard.twelvelabs.io/) configured in [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) (for video analysis tests)

## How do I run it?

```bash
cd notebook
pip install boto3 jupyter
jupyter notebook test_agentcore_deployed.ipynb
```

The notebook reads the AgentCore Runtime ARN, S3 bucket, and Memory ID from SSM Parameter Store automatically. No manual configuration needed beyond having Stack 00 deployed.

## How does the custom botocore model work?

The `bedrock-agentcore` service is not yet in the public boto3 SDK. The notebook loads a custom service model from the deployment package:

```python
CUSTOM_DATA_PATH = "../00-agent-agentcore/agent_files/deployment_package/botocore/data"

bcore = botocore.session.get_session()
loader = botocore.loaders.Loader(extra_search_paths=[CUSTOM_DATA_PATH])
bcore.register_component("data_loader", loader)
session = boto3.Session(botocore_session=bcore, region_name="us-east-1")
agentcore = session.client("bedrock-agentcore")
```

This enables calling `invoke_agent_runtime()` before the service is available in the standard SDK.

## What payload formats does the agent accept?

| Media type | Payload structure |
|------------|-------------------|
| **Text** | `{"prompt": "Hello", "actor_id": "wa-user-..."}` |
| **Image** | `{"prompt": "Describe this", "actor_id": "...", "media": {"type": "image", "format": "png", "data": "<base64>"}}` |
| **Document** | `{"prompt": "Summarize", "actor_id": "...", "media": {"type": "document", "format": "pdf", "data": "<base64>", "name": "file"}}` |
| **Video** | `{"prompt": "Analyze", "actor_id": "...", "media": {"type": "video", "s3_uri": "s3://bucket/key.mp4"}}` |
| **Audio** | `{"prompt": "Audio transcription: \"text\"", "actor_id": "..."}` (transcript as text, no media block) |

---

## Contributing

Contributions are welcome! See [CONTRIBUTING](../CONTRIBUTING.md) for more information.

---

## Security

If you discover a potential security issue in this project, notify AWS/Amazon Security via the [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## License

This library is licensed under the MIT-0 License. See the [LICENSE](../LICENSE) file for details.
