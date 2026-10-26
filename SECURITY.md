# Security policy

## Supported versions

Tiltmeter is young. Security fixes go into the latest release only.

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub: open the
repository's **Security** tab and choose **Report a vulnerability**. Include what you found,
how to reproduce it and what an attacker could do with it.

You can expect an acknowledgement within a week. We will keep you posted while we work on
a fix, agree on a disclosure date with you, and credit you in the release notes unless you
prefer otherwise.

## Threat model

Tiltmeter runs with your API credentials, talks to third-party APIs and, optionally, to an
LLM and to agents. These are the boundaries it is designed to keep:

| Concern | How Tiltmeter handles it |
|---|---|
| Credentials in the config | Kept as `${VAR}` placeholders and expanded only inside the request and notification code. Expanded values are never written to the history, passed to the LLM, put in the LangGraph state or logged, and they are scrubbed from error messages and excerpts. |
| Personal data in responses | Bodies are reduced to shapes on arrival. Only short categorical strings, such as `active`, are kept. Excerpts are stored or sent to the LLM only when `store_samples` or `llm.include_samples` is turned on. |
| Prompt injection through API responses | Responses only reach the LLM as an opt-in excerpt, wrapped in tags and labelled as untrusted data that must not be followed. Explanations are informational: nothing executes them. |
| Agents abusing the MCP server | Tools accept endpoint ids from the config, never URLs, so an agent cannot use Tiltmeter to reach other hosts. The tools that make requests are annotated as such for the client's approval prompts. |
| Webhook URLs | Treated as secrets. Delivery errors never include them. |
| Forged or tampered provider notices | Notices are applied only when an Ed25519 signature verifies against the key file at `https://<provider>/.well-known/api-change-key`, which must hold nothing but keys and comments, and every endpoint in the notice must be on the provider's domain. Redirects are followed only on `https`, on that domain and to that path. Unsigned notices are accepted only from a local file with `--allow-unsigned`. Each notice is handled once. |
| Prompt and markup injection through notices | Notices are validated strictly: no unknown members, no control characters or bidirectional overrides, short text. Their summary and notes reach the agent marked as untrusted data and reach pull requests inside a plain-text block, so they cannot add links, images or mentions. |
| Running the agent's edits | Only with `fix.test_command`, which is off by default. The command runs without a shell in the fix's worktree, with no network (a network namespace of its own on Linux, `sandbox-exec` on macOS, or not at all), only `PATH`, the locale and the variables you name, and an empty home folder. Its output reaches the agent as untrusted data, and what it writes to the worktree is discarded before the commit. It runs as your user, so it is not a sandbox for your files: use it where you would run the repository's tests anyway, such as CI. |
| Large or slow feeds | Feeds, notices and key files are fetched over `https` only, uncompressed, with a time limit per download and a size limit on the bytes received, and no credentials are sent with them. |
| The history database | A local SQLite file with no credentials. Protect it like any other local data, and keep `.tiltmeter/` out of version control. |
| Supply chain | Releases are built in GitHub Actions and published to PyPI with trusted publishing, without long-lived tokens, together with build provenance attestations. Workflow actions are pinned to commit SHAs. |

Out of scope: the configuration file itself is trusted input. Anyone who can edit it can
point Tiltmeter at any URL with your credentials, so review changes to it like code.
