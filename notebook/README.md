# Notebooks to Test the AgentCore Agent

Two notebooks to exercise the agent without a WhatsApp or Instagram frontend. Open them in JupyterLab or VS Code. Outputs are not stored in the repository; run the cells yourself.

This sample works with Amazon Bedrock AgentCore.

> Code in this repository is provided "as is", and is not officially supported by Amazon.

## What notebooks are available?

| Notebook | Purpose | Needs |
|----------|---------|-------|
| `test_agentcore_deployed.ipynb` | Calls the **deployed** [AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) with `InvokeAgentRuntime`: text, memory, idempotency, image, audio transcript, video, document, rejected payload, long-term memory records | Stack `00-agent-agentcore` deployed, AWS credentials |
| `test_multimodal_agent.ipynb` | Runs the **same agent code** locally (imports `00-agent-agentcore/agent_files`) without AgentCore Memory; includes cells that validate payloads offline | Amazon Bedrock access for the configured model |

## What are the prerequisites?

- Python 3.11 or later and a Jupyter environment (JupyterLab or VS Code).
- For the deployed notebook: [Stack 00](../00-agent-agentcore/README.md) deployed, credentials that can read the `/agentcore/*` SSM parameters, call `bedrock-agentcore:InvokeAgentRuntime` and `bedrock-agentcore:RetrieveMemoryRecords`, and `boto3` 1.43 or later. The `bedrock-agentcore` client is part of the public SDK, so the old custom botocore model is no longer needed.
- For the local notebook: the first cell installs the pinned runtime packages (`bedrock-agentcore[strands-agents]`, `strands-agents[otel]`, `twelvelabs`). Model access in [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) for `global.anthropic.claude-sonnet-4-6` (set `MODEL_ID` to change it).
- Optional for video: the TwelveLabs key stored in the stack's secret.

## What test files are included?

| File | Used for |
|------|----------|
| `imagen2.png` (about 2.4 MB) | Image test. The deployed notebook shrinks it when it is above the 1.5 MB media limit and Pillow is installed. |
| Your own `sample_video.mp4`, `sample_document.pdf`, `sample_image.jpg` | Optional; cells skip the test when the file is missing. Videos are git-ignored. |

## What does the deployed notebook test?

1. **Text and short-term memory**: the agent remembers a name and a country within one session.
2. **Idempotency**: a repeated `idempotency_key` returns the cached answer.
3. **Image, audio transcript, video, document**: the payload formats the Lambdas send.
4. **Rejected payload**: an unsupported image format never reaches the model or the memory.
5. **Long-term memory**: `RetrieveMemoryRecords` on `/facts/<actor_id>/` and `/preferences/<actor_id>/`. Extraction is asynchronous, so wait a few minutes if the lists are empty.

## What payload formats does the agent accept?

| Media type | Payload |
|------------|---------|
| Text | `{"prompt": "Hello", "actor_id": "wa-user-...", "idempotency_key": "..."}` |
| Image | add `"media": {"type": "image", "format": "png", "data": "<base64>"}` |
| Document | add `"media": {"type": "document", "format": "pdf", "data": "<base64>", "name": "file"}` |
| Video | add `"media": {"type": "video", "s3_uri": "s3://bucket/video/file.mp4"}` |
| Audio | add `"media": {"type": "audio_transcript", "data": "<text>"}` |

`actor_id` and the session ID must be at least 33 characters and different from each other. Decoded media above about 1.5 MB is rejected.

## What was validated?

The offline cells of the local notebook (payload validation and content block order) were executed with the pinned packages. The cells that call AWS (Bedrock, AgentCore, S3, TwelveLabs) were reviewed against the current API shapes (`invoke_agent_runtime`, `retrieve_memory_records` in boto3 1.43) but **not** run, because the authoring environment has no AWS account.

## Frequently asked questions

**Why does the notebook not set `runtimeUserId`?**
The agent reads the actor ID from the payload. `runtimeUserId` needs the extra `InvokeAgentRuntimeForUser` permission, which the notebook does not require.

**Why do the memory lists come back empty?**
Long-term extraction runs in the background and can take a few minutes after the conversation.

**Can I run it against a model other than the default?**
Yes. For the local notebook set `MODEL_ID` before the import cell. For the deployed runtime, redeploy stack 00 with `-c model_id=...`.

---

## Contributing

Contributions are welcome! See [CONTRIBUTING](../CONTRIBUTING.md) for more information.

---

## Security

If you discover a potential security issue in this project, notify AWS/Amazon Security via the [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## License

This library is licensed under the MIT-0 License. See the [LICENSE](../LICENSE) file for details.
