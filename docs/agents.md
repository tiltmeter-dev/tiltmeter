# Agents and MCP

Agents rarely crash when an API changes under them. They read whatever came back, fill the
gaps with plausible guesses, and carry on. Tiltmeter gives them a way to ask first: its
[MCP](https://modelcontextprotocol.io) server answers *has this API drifted?* and *which
fields should I expect?* from the same history your checks build.

- [Install the server](#install-the-server)
- [Claude Code](#claude-code)
- [Cursor](#cursor)
- [Claude Desktop](#claude-desktop)
- [LangChain and LangGraph](#langchain-and-langgraph)
- [Other MCP clients](#other-mcp-clients)
- [Tell your agent when to use it](#tell-your-agent-when-to-use-it)
- [Tools](#tools)
- [Resources](#resources)
- [From Python](#from-python)
- [Security notes](#security-notes)

## Install the server

The server is the `tiltmeter serve` command. It speaks MCP over stdio and reads the same
`tiltmeter.yaml` and `.tiltmeter/` history as the CLI.

```bash
uv tool install tiltmeter      # or: pipx install tiltmeter
```

You can also skip the install and let your client run `uvx tiltmeter serve`, which
fetches the latest release on first use.

Explanations need `ANTHROPIC_API_KEY` in the server's environment. Everything else works
without it.

## Claude Code

```bash
claude mcp add tiltmeter -- tiltmeter serve --config /absolute/path/to/tiltmeter.yaml
```

With explanations:

```bash
claude mcp add tiltmeter -e ANTHROPIC_API_KEY=sk-ant-... \
  -- tiltmeter serve --config /absolute/path/to/tiltmeter.yaml
```

To share the server with your team, add `-s project`. Claude Code then writes it to
`.mcp.json` at the project root, which you can commit. If everyone starts Claude Code from
the repository root, a relative `--config tiltmeter.yaml` works for all of them.

## Cursor

Add the server to `.cursor/mcp.json` in your project, or to `~/.cursor/mcp.json` for every
project:

```json
{
  "mcpServers": {
    "tiltmeter": {
      "command": "tiltmeter",
      "args": ["serve", "--config", "/absolute/path/to/tiltmeter.yaml"]
    }
  }
}
```

## Claude Desktop

Edit `claude_desktop_config.json`, found under `~/Library/Application Support/Claude/` on
macOS and `%APPDATA%\Claude\` on Windows:

```json
{
  "mcpServers": {
    "tiltmeter": {
      "command": "/absolute/path/to/tiltmeter",
      "args": ["serve", "--config", "/absolute/path/to/tiltmeter.yaml"],
      "env": {"ANTHROPIC_API_KEY": "sk-ant-..."}
    }
  }
}
```

Desktop apps often don't see your shell's `PATH`, so use the absolute path that
`which tiltmeter` prints.

## LangChain and LangGraph

[`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters) turns
Tiltmeter's tools into LangChain tools that any agent can use:

```python
import asyncio

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient


async def main() -> None:
    client = MultiServerMCPClient(
        {
            "tiltmeter": {
                "command": "tiltmeter",
                "args": ["serve", "--config", "/absolute/path/to/tiltmeter.yaml"],
                "transport": "stdio",
            }
        }
    )
    tools = await client.get_tools()
    agent = create_agent(
        model="anthropic:claude-opus-5-5",
        tools=tools,
        system_prompt=(
            "Before you rely on a third-party API, call has_drifted for it. "
            "If it drifted, read the summary and adapt before trusting the response."
        ),
    )
    result = await agent.ainvoke(
        {
            "messages": [
                {"role": "user", "content": "Can I still read phone from billing_customers?"}
            ]
        }
    )
    print(result["messages"][-1].content)


asyncio.run(main())
```

Install Tiltmeter as its own tool, as shown above, rather than in your agent's virtual
environment: `langchain-mcp-adapters` 0.3 requires `mcp` 1.x and Tiltmeter uses `mcp` 2.x.
They don't need to share an environment, because the server runs as a separate process,
and the two protocol versions talk to each other.

## Other MCP clients

Any client that launches stdio servers works with:

```
command: tiltmeter
args:    serve --config /absolute/path/to/tiltmeter.yaml
```

The server writes the MCP protocol to stdout and its logs to stderr.

## Tell your agent when to use it

The server ships instructions that MCP clients show to the model, but agents follow a
direct rule better. Add something like this to your system prompt, `CLAUDE.md` or
`AGENTS.md`:

```markdown
## Third-party APIs

Tiltmeter watches the external APIs this project calls (see tiltmeter.yaml).

- Before writing or running code that depends on one of them, call `has_drifted` for it.
- If it drifted, read the summary, then call `get_schema` to see the current fields
  before you change any code.
- Call `check_api` only when you need a fresh answer: it calls the real API and can
  notify the team.
```

## Tools

Paths in every answer use Tiltmeter's notation: `$` is the response body and `[*]` means
every item of an array.

### `has_drifted`

Did the endpoint's responses change at warning level or worse recently?

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint_id` | string | required | An endpoint id from the config. |
| `since` | string | 7 days ago | A duration such as `30m`, `24h`, `7d` or `2w`, or an ISO 8601 timestamp. |

It returns `drifted`, `max_severity`, `since`, `last_checked_at`, `last_check_error`, up
to 20 `changes` with the most severe first, and a one-paragraph `summary`:

```json
{
  "endpoint_id": "billing_customers",
  "drifted": true,
  "max_severity": "breaking",
  "since": "2026-10-14T10:30:00Z",
  "last_checked_at": "2026-10-21T10:00:00Z",
  "last_check_error": null,
  "changes": [
    {
      "path": "$.data[*].phone",
      "kind": "field_removed",
      "severity": "breaking",
      "before": "string",
      "after": null,
      "hint": "possibly renamed to $.data[*].phone_number",
      "id": 2,
      "check_id": 2,
      "endpoint_id": "billing_customers",
      "detected_at": "2026-10-21T10:00:00Z",
      "message": "Field $.data[*].phone (string) is no longer returned. Possibly renamed to $.data[*].phone_number."
    }
  ],
  "summary": "2 change(s) at warning level or above since 2026-10-14T10:30:00Z, worst breaking. Field $.data[*].phone (string) is no longer returned. Possibly renamed to $.data[*].phone_number."
}
```

### `get_schema`

The JSON Schema learned from the endpoint's recent responses.

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | string | An endpoint id from the config. |

It returns `observations` (how many responses the schema is built from),
`baseline_checks` and `json_schema`, which is `null` before the first check. Optional
fields are the ones missing from `required`, and enum-like fields list the values seen so
far under `x-observed-values`.

### `get_changes`

The change history, newest first.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint_id` | string | all endpoints | Only this endpoint. |
| `since` | string | all time | A duration or an ISO 8601 timestamp, as in `has_drifted`. |
| `min_severity` | string | `info` | `info`, `warning` or `breaking`. |
| `limit` | integer | `50` | From 1 to 500. |

### `list_endpoints`

Every configured endpoint with its method, URL without the query string, description,
and the time, outcome and worst severity of its last check. It takes no parameters.

### `check_api`

Calls one endpoint now, compares the response with its baseline and records the result,
exactly like `tiltmeter check -e <id>`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint_id` | string | required | An endpoint id from the config. |
| `explain` | boolean | `true` | Ask Claude to explain drift, when the server has an API key. |

It returns the full check result: `outcome`, `changes`, `explanation`, `error`, `notes`,
status code and latency. Configured notifications fire as usual.

### `check_all`

Calls every endpoint now and returns one result each. It takes `explain` like `check_api`.

### Annotations and errors

`has_drifted`, `get_schema`, `get_changes` and `list_endpoints` are annotated as
read-only, so clients can run them without asking. `check_api` and `check_all` are
annotated as reaching the outside world. An unknown endpoint id or a malformed `since`
comes back as a tool error with a readable message, such as
`unknown endpoint 'billing' (configured endpoints: billing_customers)`.

## Resources

| URI | Contents |
|---|---|
| `tiltmeter://endpoints` | The same list as `list_endpoints`, as JSON. |
| `tiltmeter://schema/{endpoint_id}` | The learned JSON Schema of one endpoint. |

## From Python

The service behind the CLI and the MCP server can be used directly. This API may still
change before 1.0.

```python
import asyncio
from pathlib import Path

from tiltmeter.service import DriftService


async def main() -> None:
    async with DriftService.from_config_file(Path("tiltmeter.yaml")) as service:
        for result in await service.check(explain=False):
            print(result.endpoint_id, result.outcome, result.max_severity)
        print(service.drift_status("billing_customers").summary)


asyncio.run(main())
```

## Security notes

- The tools take endpoint ids, never URLs, so an agent cannot point Tiltmeter at an
  arbitrary address.
- `check_api` and `check_all` make real requests with your credentials and can send
  notifications. Leave them out of the auto-approved tools if that matters to you.
- Answers never contain credentials or response bodies. The values they can contain are
  short enum-like strings, such as a new `status` value.
