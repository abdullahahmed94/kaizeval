# Cloud platforms: AWS and Salesforce Agentforce

Kaizeval can test agents that run on a cloud platform, and can use a cloud model as its own
reviewer, without any extra SDK. Four built-in integrations:

| What | Kind / adapter | Platform API it calls |
|---|---|---|
| The model Kaizeval uses (tests, reviews, root cause, proposals) | provider kind `aws_bedrock` | Amazon Bedrock `Converse` |
| An agent built with Amazon Bedrock Agents | adapter `aws_bedrock_agent` | `InvokeAgent` |
| An agent hosted on Amazon Bedrock AgentCore Runtime | adapter `aws_agentcore` | `InvokeAgentRuntime` |
| A Salesforce Agentforce agent | adapter `salesforce_agentforce` | Agentforce Agent API |

> **How this is verified.** Automated tests exercise each integration against local stand-ins
> that speak the platform's wire format, and the AWS request signature is checked against the
> reference implementation in `botocore`. The Agentforce connector has also completed a full
> `kaizeval run` against an activated agent in a sandbox org. If a call fails, the error message
> names the platform call and the host.

## What you can and cannot do with a hosted agent

All three agent adapters are **black-box**: Kaizeval sends each test conversation to the deployed
agent and reads the answer.

- Available: generated and imported tests, single / dual / committee review, root-cause
  findings, recommendations, reports, comparing two runs.
- Not available: the automatic improvement loop. It needs an isolated candidate copy that
  Kaizeval may change, and these platforms do not expose one through their runtime API. Apply
  the recommendations in the platform's own builder, then run the tests again. To compare two
  versions, register each one (an alias, an endpoint, a second agent) as its own agent.

Three things to know before the first run:

1. **Tests are real conversations.** If the agent can take actions (open a case, issue a refund,
   send an email), they really happen. Use a sandbox org, a test alias or a test agent.
2. **The platform bills the usage.** Start with a small test set (`tests.cases`), and set
   `evaluation.max_agent_calls`.
3. **The workspace must be in connected mode**, and the agent must be marked as being on a
   public address (`allow_public`). An offline workspace refuses these endpoints before any
   request is sent. A private (VPC) endpoint can be given as `endpoint_url` instead.

A multi-turn test case is replayed in one fresh session: the user turns are sent in order and the
answer to the last one is evaluated. The reported latency is that of the last turn.

## AWS credentials

The credential is stored encrypted in the workspace and is never shown again. Accepted forms:

- `ACCESS_KEY_ID:SECRET_ACCESS_KEY`
- `ACCESS_KEY_ID:SECRET_ACCESS_KEY:SESSION_TOKEN` (temporary credentials)
- a JSON object with `access_key_id`, `secret_access_key` and optionally `session_token`
- for the model provider only: an Amazon Bedrock API key

When no credential is stored, Kaizeval reads `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and
`AWS_SESSION_TOKEN` (or `AWS_BEARER_TOKEN_BEDROCK`) from the environment of the Kaizeval server.
Shared credential files, SSO profiles and instance roles are not read; export temporary
credentials into the environment instead.

Use an IAM identity that can do only what is needed:

| Integration | IAM action |
|---|---|
| `aws_bedrock` model | `bedrock:InvokeModel` on the model or inference profile |
| `aws_bedrock_agent` | `bedrock:InvokeAgent` on the agent alias |
| `aws_agentcore` | `bedrock-agentcore:InvokeAgentRuntime` on the runtime |

## Amazon Bedrock as the model

Console: **Models → Add a model provider → Amazon Bedrock**. Project file:

```yaml
network: connected
model:
  kind: aws_bedrock
  region: us-east-1
  model: <model id or inference profile id>   # exactly as Amazon Bedrock lists it
  api_key_env: KAIZEVAL_AWS_CREDENTIAL         # holds ACCESS_KEY_ID:SECRET_ACCESS_KEY; omit to use the AWS_* variables
  allow_public: true
  max_tokens: 8000
```

The model must be enabled for your account in that Region. A model that refuses a `temperature`
is detected on the first call and the field is left out from then on. `base_url` may point to a
private endpoint.

## Amazon Bedrock agent

Console: **Agents → Register an agent → Amazon Bedrock agent**. Project file:

```yaml
network: connected
agent:
  platform: aws_bedrock_agent
  region: us-east-1
  agent_id: ABCDEFGHIJ
  agent_alias_id: TSTALIASID        # TSTALIASID is the working draft
  credential_env: KAIZEVAL_AWS_CREDENTIAL
  allow_public: true
```

Knowledge-base sources that the agent attributes its answer to are read as citations (the S3
URI, web URL or document id), so the citation checks work. An agent that returns control to the
caller for an action fails that test case with `NON_REPLAYABLE_TOOL`: Kaizeval does not carry out
customer actions on behalf of the agent.

Amazon Bedrock Agents is closed to new AWS customers; AWS points new projects to AgentCore.

## Amazon Bedrock AgentCore agent

```yaml
network: connected
agent:
  platform: aws_agentcore
  agent_runtime_arn: arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/my_agent-AbCdEf1234
  # qualifier: DEFAULT              # the runtime endpoint to test
  # message_field: prompt           # the JSON field your agent reads the question from
  # response_path: result.content.0.text   # where the answer is in the reply of your agent
  # citations_path: citations
  credential_env: KAIZEVAL_AWS_CREDENTIAL
  allow_public: true
```

An AgentCore agent defines its own request and reply. Kaizeval sends `{"prompt": "<question>"}`
by default and looks for the answer under the usual names (`answer`, `result`, `response`,
`output`, `message`, `text`, `content`). Set `message_field` and `response_path` when your agent
differs. JSON and server-sent-event replies are both read. Agents that accept only OAuth bearer
tokens (inbound JWT authorization) or the MCP protocol are not supported by this adapter.

## Salesforce Agentforce agent

In Salesforce Setup, once:

1. Create a connected app (or external client app) with the **client credentials flow** enabled,
   a run-as user, and the OAuth scopes that the Salesforce Agent API guide lists (at the time of
   writing: `api`, `refresh_token, offline_access`, `chatbot_api`, `sfap_api`).
2. Add that connected app to the agent's **Connections**.
3. Activate the agent. Note its id (the 18 characters at the end of its URL in Setup) and your
   My Domain URL. The Agent API does not serve the *Agentforce (Default)* agent type.

Console: **Agents → Register an agent → Salesforce Agentforce agent**. Project file:

```yaml
network: connected
agent:
  platform: salesforce_agentforce
  my_domain: https://yourcompany.my.salesforce.com
  agent_id: 0XxXXXXXXXXXXXXXXX
  client_id_env: KAIZEVAL_SF_CONSUMER_KEY       # holds the consumer key (or write it here as client_id)
  credential_env: KAIZEVAL_SF_CONSUMER_SECRET   # holds the consumer secret
  allow_public: true
  # bypass_user: true      # false runs the agent as the token's user instead of the agent's own user
  # variables: []          # context variables sent with every message
```

Per test case Kaizeval gets a token from your My Domain, starts a session on
`api.salesforce.com`, sends the user turns, and ends the session. The token is reused and
refreshed when it expires. Both hosts must be reachable from the Kaizeval server.

## Other platforms

Any agent that answers over HTTP works with the generic `http_chat` adapter, and any model
behind an OpenAI-compatible endpoint works with the `openai_compatible` provider. A platform
with its own protocol can be added as an adapter: see [CONNECTOR_SDK.md](CONNECTOR_SDK.md).
