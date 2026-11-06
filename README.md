# Multichannel AI Agent for WhatsApp and Instagram with Amazon Bedrock AgentCore

| Voice notes | Image |
|----------|------------|
|![Voice notes](./images/voice-note.gif)| ![Image](./images/image-whatsapp-agent.gif) |

| Video | Instagram |
|--------------|--------------|
|![Video](./images/video-whatsapp-agent.gif)|![Instagram](./images/chat-instagram-agent.gif)|

A channel-agnostic multimodal AI agent for text, images, audio, video and documents, reachable from WhatsApp and Instagram. It is built with [Strands Agents](https://strandsagents.com/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) and runs on [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) Runtime with AgentCore Memory, so one deployed agent remembers a person across channels.

This sample works with Amazon Bedrock AgentCore.

> Code in this repository is provided "as is", and is not officially supported by Amazon.

> **Demo project**: this sample illustrates architecture and integration patterns for learning. It already includes webhook signature verification, scoped IAM and idempotent message handling, but it has not been through a production readiness review, load test or live validation of every path (see [What was not validated?](#what-was-not-validated)). Review security, cost and operations before you adapt it.

## Projects

| Project | Description | Stack |
|---------|-------------|-------|
| [00-agent-agentcore](./00-agent-agentcore/) | AgentCore Runtime (direct code deploy) with the Strands multimodal agent, AgentCore Memory, media bucket and TwelveLabs secret. Publishes its ARNs to SSM Parameter Store. | ![Python](https://img.shields.io/badge/Python-3.13-blue) ![AgentCore](https://img.shields.io/badge/AWS-AgentCore-orange) ![Strands](https://img.shields.io/badge/Strands-Agents-green) |
| [01-whatsapp-end-user-messaging](./01-whatsapp-end-user-messaging/) | WhatsApp through [AWS End User Messaging Social](https://aws.amazon.com/end-user-messaging/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el): SNS, Lambda, DynamoDB Stream buffering, AgentCore. | ![Python](https://img.shields.io/badge/Python-3.13-blue) ![CDK](https://img.shields.io/badge/AWS-CDK-orange) ![SNS](https://img.shields.io/badge/AWS-SNS-yellow) |
| [02-multichannel-api-gateway](./02-multichannel-api-gateway/) | WhatsApp and Instagram DM through the [Meta Cloud API](https://developers.facebook.com/docs/graph-api/): one API Gateway webhook with HMAC signature verification, Lambda, DynamoDB Stream buffering, AgentCore, unified users table. | ![Python](https://img.shields.io/badge/Python-3.13-blue) ![CDK](https://img.shields.io/badge/AWS-CDK-orange) ![APIGW](https://img.shields.io/badge/AWS-API_Gateway-purple) ![Instagram](https://img.shields.io/badge/Meta-Instagram-E4405F) |
| [notebook](./notebook/) | Notebooks to test the deployed runtime, or the agent code locally, without a messaging channel. | ![Jupyter](https://img.shields.io/badge/Jupyter-Notebook-orange) |

## How is the solution built?

![Architecture overview: three CDK stacks. Stack 00 deploys the AgentCore Runtime and Memory. Stack 01 connects WhatsApp through SNS. Stack 02 connects WhatsApp and Instagram through API Gateway. Both channel stacks buffer messages in DynamoDB before calling the runtime](./images/diagram.svg)

Choose one channel stack after stack 00: stack 01 if you use AWS End User Messaging Social for WhatsApp, stack 02 if you use a Meta app for WhatsApp and Instagram. The stacks are independent CDK apps that share configuration through SSM parameters (`/agentcore/*`).

## What changed in this version?

The sample was audited against the current Strands Agents, AgentCore, Lambda, API Gateway and Meta documentation. The full list, with sources and final statuses, is in [docs/AUDIT.md](./docs/AUDIT.md).

| Area | Now |
|------|-----|
| Model | `global.anthropic.claude-sonnet-4-6` by default (the previous Sonnet 4 default reached end of life on 2026-10-14). Change it with `-c model_id=...`. |
| Runtime | AgentCore Runtime on Python 3.13 (arm64). Lambdas on Python 3.13 (arm64). |
| Agent | One Strands agent per request, conversation window, turn and token limits, prompt caching, optional Bedrock Guardrail, tool-safety plugin, JSON logs with hashed IDs. |
| Memory | Facts, preferences, summaries and episodes with explicit namespaces, 30 day event expiry. |
| Webhook security | `X-Hub-Signature-256` verified with the Meta app secret (`WHATS_APP_SECRET`, `IG_APP_SECRET`), no CORS, throttling, request validation. |
| Reliability | Conditional-put deduplication (Meta retries for up to 36 hours), per-message claims, stream retry limits, bisect and SQS failure destination. |
| IAM | Scoped to the runtime, the model and the resource name patterns. No wildcard resources except the two actions that do not support resource scoping. |
| Tests | 76 + 41 + 72 offline tests, and `cdk synth` for all three stacks without AWS credentials. |

## Why is this multichannel by design?

The AgentCore Runtime and Memory layer knows nothing about WhatsApp. Any system that can call [InvokeAgentRuntime](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) can be a frontend.

### How does cross-channel memory work?

AgentCore Memory is keyed by two identifiers. The `actor_id` identifies the person and drives long-term memory (facts, preferences, summaries, episodes). The `session_id` identifies the conversation and drives short-term memory. If two frontends send the same `actor_id` for the same person, the agent remembers that person on both. Stack 02 does this with a unified users table: when a user links WhatsApp and Instagram, both resolve to one `actor_id`.

Both IDs must be different strings of at least 33 characters; the Lambdas pad them with `ljust(33, "0")`.

### Which channels can connect?

| Channel | Integration path | `actor_id` strategy |
|---------|-----------------|---------------------|
| WhatsApp (End User Messaging Social) | SNS, Lambda, AgentCore (stack 01) | `wa-user-{phone}` |
| WhatsApp (Meta Cloud API) | API Gateway, DynamoDB, Lambda, AgentCore (stack 02) | `wa-user-{phone}`, or the linked user id |
| Instagram Direct Messages | Same webhook (stack 02) | `ig-user-{instagram_scoped_id}`, or the linked user id |
| Telegram, Messenger, web chat, mobile app | Your own receiver that calls AgentCore | Your canonical user id |

## How does multimedia memory work?

AgentCore Memory stores text only, so media becomes text before it is remembered.

1. **Image**: Claude vision reads it as an inline content block and the description enters memory.
2. **Audio**: [Amazon Transcribe](https://aws.amazon.com/transcribe/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) turns it into text in the Lambda and the transcript goes to the agent.
3. **Video**: the `video_analysis` tool calls the [TwelveLabs](https://www.twelvelabs.io/) API. The agent keeps a `[VIDEO: id=... | desc="..."]` tag for follow-up questions.
4. **Document**: the agent reads PDF, DOCX and similar files as inline blocks and stores a summary.

| Media | Formats | Limits |
|-------|---------|--------|
| Image | JPEG, PNG, GIF, WebP | About 1.5 MB decoded sent to the agent. |
| Document | PDF, CSV, DOC, DOCX, XLS, XLSX, HTML, TXT, MD | About 1.5 MB decoded. |
| Audio | OGG, MP3, AAC, M4A, WAV, AMR | Transcribed by Amazon Transcribe. |
| Video | MP4, MOV, MKV, WebM, FLV, MPEG, 3GP | Analyzed by TwelveLabs through a presigned S3 URL. |

Larger images and documents get a friendly "too large" reply instead of reaching the runtime, because the `InvokeAgentRuntime` payload is limited.

## How does message buffering work?

People send several short messages in a row. Without buffering each one starts a separate agent call. Stacks 01 and 02 write each message to DynamoDB (partition key `from_phone`, sort key `id`) and read the table stream with a Lambda [tumbling window](https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-windows.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el). The window is 20 seconds in stack 01 and 10 seconds in stack 02 (the `buffer_seconds` argument of the Lambda construct). The processor groups messages by sender, joins the text and calls the agent once.

The pattern comes from [sample-whatsapp-end-user-messaging-connect-chat](https://github.com/aws-samples/sample-whatsapp-end-user-messaging-connect-chat), which reports roughly 4 messages per aggregate. That ratio was not measured in this repository; your saving depends on how your users type.

Retries are safe. A conditional put on `(from_phone, id)` makes Meta's redeliveries harmless, and the processor claims each message before it works on it.

## How do AgentCore Runtime sessions work?

Each [session](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) runs in an isolated microVM for up to 8 hours and ends after 15 minutes without requests. After you redeploy the agent, wait for the idle timeout or use a new `session_id` to get the new code.

## How do I test it without a messaging channel?

Both [notebooks](./notebook/) run without WhatsApp or Instagram. `test_agentcore_deployed.ipynb` invokes the deployed runtime. `test_multimodal_agent.ipynb` runs the agent code on your machine without memory. Open them in JupyterLab or VS Code.

For automated checks, each stack has an offline test suite and the repository has a hygiene check:

```bash
cd 00-agent-agentcore   # or 01-... or 02-...
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt   # stack 00 also installs the runtime packages, see its README
python -m pytest -q

export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1
npx --yes aws-cdk@latest synth --quiet   # no AWS account needed

cd .. && python scripts/check_repo.py    # placeholders, TRK links, em dashes, retired model ID, README sections, notebook outputs
```

## How do I deploy?

Deploy in order. The channel stacks read the SSM parameters that stack 00 writes.

1. **Stack 00** (required): `cd 00-agent-agentcore`, install, `npx aws-cdk@latest deploy`. Then store the TwelveLabs key:
   ```bash
   aws secretsmanager put-secret-value --secret-id <TwelveLabsSecretArn output> \
     --secret-string '{"TL_API_KEY":"<your key>"}'
   ```
2. **Stack 01 or stack 02**: follow the README of the stack for the WhatsApp or Meta setup, then `npx aws-cdk@latest deploy`.
3. **Optional**: enable [CloudWatch Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) once per account to see AgentCore traces.

### What are the prerequisites?

- [AWS CLI](https://aws.amazon.com/cli/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) with credentials, and a [CDK bootstrapped](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) account and Region.
- Node.js (for `npx aws-cdk@latest`), Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Stacks 00 and 02 call `uv` during `cdk synth` to build the arm64 packages, so the first synth needs network access.
- Amazon Bedrock model access for `global.anthropic.claude-sonnet-4-6` (or your `model_id`).
- Stack 01: [AWS End User Messaging Social](https://aws.amazon.com/end-user-messaging/?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) with a WhatsApp Business number. Stack 02: a [Meta Developer](https://developers.facebook.com/) app with WhatsApp, and for Instagram a Professional account linked to Meta Accounts Center.
- Optional: a [TwelveLabs](https://www.twelvelabs.io/) API key for video.

## How is it observed?

The runtime sends OpenTelemetry data through the AWS Distro for OpenTelemetry and writes JSON logs to CloudWatch Logs. Logs contain hashed IDs, channel, media type and exception types, never message text or phone numbers. AgentCore traces and spans show up in CloudWatch after you enable Transaction Search once per account (see step 3 above). Lambdas have active X-Ray tracing and 30 day log groups.

## What are the known limitations?

- **Payload size**: images and documents above about 1.5 MB are refused with a friendly message. Resize them before sending.
- **Memory extraction is asynchronous**: facts from a conversation can take a few minutes to appear. When you test cross-channel memory, wait 2 to 3 minutes before switching channels.
- **Retrieval is semantic**: a vague question such as "what did I send you?" may not retrieve a specific image fact. The agent prompt asks the model to write explicit "Fact:" lines to help extraction.
- **Invalid content in memory is permanent**: it cannot be deleted per record, so all media is validated before it reaches the agent.
- **One call per agent instance**: Strands raises `ConcurrencyException` on overlapping calls. The runtime builds one agent per request to avoid it.
- **Session caching**: a microVM keeps state for up to 8 hours or 15 idle minutes. If you delete rows from the users table while testing, use a new `session_id`.
- **Account linking trusts chat input**: the `link_account` tool accepts identifiers typed by the user. Set `LINK_ALLOW_MERGE=false` on the runtime or add out-of-band verification before real use.
- **Public runtime endpoint with SigV4**: the runtime uses PUBLIC network mode with IAM authentication, called only by your Lambdas. Use VPC mode if you need private networking.

## What was not validated?

The changes in this version were verified offline: unit tests, CDK assertion tests and `cdk synth` with fake credentials. These paths need a real account or provider and were **not** run live:

- Deploying any stack, and the AgentCore Runtime starting with the new Python 3.13 package.
- Calls to Bedrock, AgentCore Memory (strategies, namespaces, retrieval), TwelveLabs, Amazon Transcribe and End User Messaging Social.
- Real Meta webhooks: signature verification is tested with computed signatures, not with traffic from Meta. The accepted `metaApiVersion` values of End User Messaging Social are unconfirmed; `v25.0` is the default and you can set `-c meta_api_version=`.
- The notebooks' calls to AWS (their offline cells were run).
- Cost, latency and the buffering savings figure.

See [docs/AUDIT.md](./docs/AUDIT.md), section "Deferred / not validated offline", for the complete list.

## What are good next steps?

AgentCore offers services that this sample does not deploy yet:

- [AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) to expose business APIs and Lambda functions to the agent as MCP tools.
- [AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) to manage outbound credentials for those tools, and inbound authentication if you move away from SigV4.
- [AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) to score real conversations and catch regressions after a model or prompt change.
- VPC network mode for the runtime and a Bedrock Guardrail (`-c guardrail_id=...` is already supported).

## Troubleshooting

| Symptom | Resolution |
|---------|------------|
| SSM parameter not found at deploy | Deploy stack 00 first, in the same account and Region. |
| `cdk synth` fails on stack 00 or 02 at the build step | Install `uv` and allow network access, or pass a prebuilt package with `-c package_path=...` (stack 00) or `-c layer_path=...` (stack 02). |
| Webhook returns 403 | The Meta app secret in Secrets Manager is missing, still a placeholder, or does not match the app that sends the webhook. |
| Agent does not use new code | Wait 15 minutes or use a new `session_id`. |
| `RuntimeClientError` or 500 from AgentCore | The processor retries with backoff. It usually happens on a cold start; check the runtime logs. |
| Cross-channel memory is empty | Extraction is asynchronous; wait a few minutes. |
| No traces in CloudWatch | Enable Transaction Search once per account. |

## Frequently asked questions

**Can the same agent serve WhatsApp and Instagram?**
Yes. Stack 02 receives both on one webhook, detects the channel from the payload and sends the replies through the right API.

**Does the agent remember me across channels?**
Yes, after the account is linked. The unified users table maps the WhatsApp phone and the Instagram ID to one canonical ID that becomes the memory `actor_id`.

**What happens if I deploy only stack 01?**
You get a WhatsApp-only assistant. Linking and Instagram need stack 02, and the agent only offers linking when the user ID tag is present.

**How do I use a different model?**
Pass `-c model_id=<model or inference profile ID>` to `cdk deploy` in stack 00. IAM follows the model automatically. Check [model lifecycle](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) before you pick one.

**Can I add Telegram or a web chat?**
Yes. Write a receiver that normalizes the message, keep the same payload and call `InvokeAgentRuntime`.

**Can I use another agent framework?**
The pattern also fits other frameworks. This sample uses Strands Agents for its tool and memory integration.

---

## Contributing

Contributions are welcome! See [CONTRIBUTING](CONTRIBUTING.md) for more information.

---

## Security

If you discover a potential security issue in this project, notify AWS/Amazon Security via the [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file for details.

---

## Connect with me

🇻🇪

[![Dev.to](https://img.shields.io/badge/Dev.to-@elizabethfuentes12-0A0A0A?style=for-the-badge&logo=dev.to&logoColor=white)](https://dev.to/elizabethfuentes12)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-lizfue-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://www.linkedin.com/in/lizfue/)
[![GitHub](https://img.shields.io/badge/GitHub-elizabethfuentes12-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/elizabethfuentes12/)
[![X](https://img.shields.io/badge/Twitter-@ElizabethFue12-1DA1F2?style=for-the-badge&logo=twitter&logoColor=white)](https://x.com/ElizabethFue12)
[![Instagram](https://img.shields.io/badge/Instagram-@elifue.tech-E4405F?style=for-the-badge&logo=instagram&logoColor=white)](https://www.instagram.com/elifue.tech)
[![YouTube](https://img.shields.io/badge/YouTube-Channel-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/channel/UCr0Gnc-t30m4xyrvsQpNp2Q?sub_confirmation=1)
