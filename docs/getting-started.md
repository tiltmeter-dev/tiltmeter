# Getting started

This tour runs a full drift cycle on your machine: a fake billing API, a baseline, a vendor
change, an alert, an agent that can ask about it, and one that fixes the code. It takes
about five minutes and needs Python 3.12 or newer.

## 1. Install

```bash
pip install tiltmeter          # or: uv tool install tiltmeter
tiltmeter --version
```

## 2. Start a fake API

Create a folder with one JSON file and serve it. Any static file server works; Python
ships one.

```bash
mkdir tiltmeter-tour && cd tiltmeter-tour
mkdir api
cat > api/customers.json <<'EOF'
{
  "object": "list",
  "data": [
    {"id": "cus_1001", "email": "ada@example.com", "phone": "+1 555 0100", "balance": 1200},
    {"id": "cus_1002", "email": "alan@example.com", "phone": "+1 555 0101", "balance": 300}
  ],
  "has_more": false
}
EOF
python -m http.server 8765 --directory api
```

Leave the server running and open a second terminal in the same folder.

## 3. Describe the endpoint

```bash
cat > tiltmeter.yaml <<'EOF'
endpoints:
  - id: billing_customers
    description: Customer list from the billing API
    url: http://localhost:8765/customers.json
EOF
```

`tiltmeter init` writes a longer starter file with commented examples. This one is all
the tour needs. For a real web app, `tiltmeter init --from-har` builds the file from a
browser export instead; see [Configuration](configuration.md#from-a-har-file).

## 4. Record the baseline

Run two checks:

```bash
tiltmeter check
tiltmeter check
```

```console
● billing_customers  200 · 4 ms  baseline recorded (first check)
1 endpoint checked · 1 baseline
✓ billing_customers  200 · 2 ms  no drift
1 endpoint checked
```

The first check has nothing to compare against, so it only learns the shape. The second
one compares and finds nothing new.

## 5. Change the API

Play the vendor: rename `phone` to `phone_number` and turn `balance` into a string.

```bash
cat > api/customers.json <<'EOF'
{
  "object": "list",
  "data": [
    {"id": "cus_1001", "email": "ada@example.com", "phone_number": "+1 555 0100", "balance": "1200"},
    {"id": "cus_1002", "email": "alan@example.com", "phone_number": "+1 555 0101", "balance": "300"}
  ],
  "has_more": false
}
EOF
tiltmeter check; echo "exit code: $?"
```

```console
✗ billing_customers  200 · 3 ms  BREAKING · 3 changes
    breaking  type_changed   $.data[*].balance       integer → string
    breaking  field_removed  $.data[*].phone         string → (absent)  possibly renamed to $.data[*].phone_number
    info      field_added    $.data[*].phone_number  (absent) → string  possibly replaces $.data[*].phone
    note: LLM explanation skipped: set ANTHROPIC_API_KEY to explain drift with Claude
1 endpoint checked · 1 breaking
exit code: 2
```

Exit code 2 means breaking drift, which is what fails a CI job. Run `tiltmeter check`
once more and it reports `no drift`: each change is reported once, then it becomes part of
the baseline.

## 6. Look around

```bash
tiltmeter history                     # every check, newest first
tiltmeter show 3                      # the check that found the drift
tiltmeter schema billing_customers    # the JSON Schema Tiltmeter has learned
```

Add `--json` to `history` and `show` for machine-readable output.

## 7. Ask Claude what happened

With an Anthropic API key, Tiltmeter explains drift as it finds it. You can also explain a
past check:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
tiltmeter explain 3
```

The explanation covers what changed, the likely cause, the impact on consumers, what to do
and which fields were probably renamed. The model sees field names and types, never your
response bodies.

## 8. Let an agent ask

Register the MCP server with Claude Code, using the absolute path of your config:

```bash
claude mcp add tiltmeter -- tiltmeter serve --config "$PWD/tiltmeter.yaml"
```

Then ask Claude Code something like *"Has the billing_customers API drifted? What should I
change in my client?"*. It calls `has_drifted` and `get_schema` and answers from the same
history you just built. Other clients are covered in the [agents guide](agents.md).

## 9. Let an agent fix the code

Somewhere, code still reads `phone`. Create a tiny client that does, in its own git
repository:

```bash
mkdir billing-client
cat > billing-client/client.py <<'PY'
import httpx


def contact_lines() -> list[str]:
    customers = httpx.get("http://localhost:8765/customers.json").json()["data"]
    return [f"{c['email']} <{c['phone']}>" for c in customers]
PY
git -C billing-client init -q && git -C billing-client add . && git -C billing-client commit -qm "Billing client"
```

Then hand check 3 to an agent. With [Claude Code](https://code.claude.com) installed and
logged in, nothing else is needed:

```bash
tiltmeter fix 3 --repo billing-client --agent claude-code --dry-run
```

```console
Working on a new branch, tiltmeter/fix-billing-customers-3, from main at b3593b4.
1 file changed  +1 -1
diff --git a/client.py b/client.py
-    return [f"{c['email']} <{c['phone']}>" for c in customers]
+    return [f"{c['email']} <{c['phone_number']}>" for c in customers]
╭─ agent ─────────────────────────────────────────────────────────────────────────╮
│ Renamed the customer field read from `phone` to `phone_number` in client.py.    │
│ Nothing in the repository reads `balance`, so that type change needed no edit.  │
│ confidence high                                                                 │
╰─────────────────────────────────────────────────────────────────────────────────╯
discarded the edits; nothing was committed
```

Without Claude Code, use the built-in agent: `pip install "tiltmeter[fix]"`, set
`ANTHROPIC_API_KEY` and drop `--agent claude-code`. Either way the agent works in a
temporary copy of the repository with file tools only. `--dry-run` shows the diff and
throws it away. Without it, Tiltmeter asks before it commits, pushes and opens a pull
request. The [fix guide](fix.md) has the details.

## Next steps

- Point Tiltmeter at the APIs you actually depend on: [configuration reference](configuration.md).
- Run it on a schedule and fail builds on breaking drift: [CI guide](ci.md).
- Get alerts in Slack: see `notify` in the [configuration reference](configuration.md#notifications).
- Fix code in your real repositories, and review the pull requests: [fix guide](fix.md).
- Understand what is and is not reported: [how it works](how-it-works.md).

When you are done, stop the server with Ctrl+C. Everything Tiltmeter learned lives in
`.tiltmeter/` next to `tiltmeter.yaml`; delete that folder to start over.
