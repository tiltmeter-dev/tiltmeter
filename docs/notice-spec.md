# API change notices, version 1

An API change notice is a small JSON document in which an API provider describes a change
to the JSON responses of its API once, in a form that tools on the customer's side can
act on without reading prose: find the code that reads the changed fields and adapt it.
This page specifies the notice, the feed that lists notices, and how notices are signed.
It is an open format: Tiltmeter implements it, and anyone else may too.

- **Notice schema:** [`https://tiltmeter.dev/schemas/api-change-notice/v1.json`](schemas/api-change-notice/v1.json)
- **Feed schema:** [`https://tiltmeter.dev/schemas/api-change-feed/v1.json`](schemas/api-change-feed/v1.json)
- **Status:** version 1. Changes that break readers get a new version and a new schema URL.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

- [A notice](#a-notice)
- [Changes](#changes)
- [Signatures](#signatures)
- [Feeds](#feeds)
- [Reading notices](#reading-notices)
- [Security considerations](#security-considerations)

## A notice

```json
{
  "spec": "https://tiltmeter.dev/schemas/api-change-notice/v1.json",
  "id": "billing-2026-11-03-phone-rename",
  "provider": "api.partner.example",
  "published_at": "2026-11-03T12:00:00Z",
  "effective_at": "2026-12-01T00:00:00Z",
  "summary": "Customers now return phone_number instead of phone, and balance as a decimal string.",
  "endpoints": [
    {
      "method": "GET",
      "url": "https://api.partner.example/v1/customers",
      "changes": [
        { "path": "$.data[*].phone", "kind": "field_removed", "renamed_to": "$.data[*].phone_number" },
        { "path": "$.data[*].balance", "kind": "type_changed", "before": "integer", "after": "string",
          "note": "Decimal string in the account currency, such as \"12.00\"." }
      ]
    }
  ],
  "sdks": [{ "ecosystem": "pypi", "name": "partner-sdk", "fixed_in": "3.0.0" }],
  "signature": {
    "alg": "ed25519",
    "key": "https://api.partner.example/.well-known/api-change-key",
    "value": "<base64 of the 64-byte signature>"
  }
}
```

| Member | Required | Description |
|---|---|---|
| `spec` | yes | Exactly `https://tiltmeter.dev/schemas/api-change-notice/v1.json`. |
| `id` | yes | 1 to 100 letters, digits, `.`, `_` or `-`, starting with a letter or digit. Unique among the provider's notices and never reused for a different change. |
| `provider` | yes | The provider's domain name, in lowercase, such as `api.partner.example` or `partner.example`. Every endpoint URL MUST be on this domain or one of its subdomains, and the signing key is served from this exact host. |
| `published_at` | yes | When the notice was published, as an RFC 3339 time with an offset. |
| `effective_at` | no | When the old responses stop. Tools SHOULD show it, for example in the pull request title. |
| `summary` | yes | 1 to 500 characters of plain text for humans. |
| `endpoints` | yes | 1 to 50 [endpoints](#endpoints). |
| `sdks` | no | Up to 20 [SDK releases](#sdk-releases) that already follow the change. |
| `signature` | no | The [signature](#signatures). Tools that act without a human MUST ignore notices without a valid one. |

No other members are allowed, at any level. Text members (`summary`, `note`, `before`,
`after`) MUST NOT contain control characters (U+0000 to U+001F and U+007F to U+009F) or
bidirectional overrides (U+202A to U+202E and U+2066 to U+2069). Paths (`path`,
`renamed_to`) MUST NOT contain those or backticks, and URLs MUST consist of the
characters RFC 3986 allows, plus `{` and `}` for path parameters. A notice holds no
fractional numbers.

### Endpoints

| Member | Required | Description |
|---|---|---|
| `method` | yes | `GET`, `POST`, `PUT`, `PATCH` or `DELETE`. |
| `url` | yes | The `https` URL customers call, without query string, fragment or credentials. `{name}` marks a path parameter, as in OpenAPI: `https://api.partner.example/v1/customers/{id}`. |
| `changes` | yes | 1 to 200 [changes](#changes) to the responses of that endpoint. |

### SDK releases

| Member | Required | Description |
|---|---|---|
| `ecosystem` | yes | `pypi`, `npm`, `go`, `maven`, `nuget`, `rubygems`, `cargo` or `packagist`. |
| `name` | yes | The package name as that ecosystem spells it: `partner-sdk`, `@partner/sdk`, `github.com/partner/sdk-go`, `com.partner:sdk`. |
| `fixed_in` | yes | The first release that follows the change, such as `3.0.0` or `v3.0.0`. |

## Changes

A change uses the same vocabulary as Tiltmeter's drift reports, so a notice and an
observed drift describe a change in the same terms.

| Member | Required | Description |
|---|---|---|
| `path` | yes | Where in the response body, in the path syntax below. |
| `kind` | yes | One of the kinds below. |
| `before` | no | The JSON type, or the values, before: `integer`, `string \| null`, `active, past_due`. At most 200 characters. |
| `after` | no | The same, after. |
| `renamed_to` | no | The path where a removed field lives on. Only with `field_removed`. |
| `note` | no | Up to 500 characters in the provider's own words, such as the format of a new string. |

| Kind | Meaning |
|---|---|
| `field_removed` | The field is no longer returned. With `renamed_to`, it is returned under that path instead. |
| `field_now_optional` | The field used to be in every response and is now missing from some. |
| `type_changed` | The field returns a type it did not return before. |
| `nullable_introduced` | The field can now be `null`. |
| `enum_value_added` | The field returns values outside the ones it used to. |
| `field_added` | A new field. Informational; tools need not act on it. |
| `status_changed` | The HTTP status of a successful call changes; `before` and `after` are status codes. |
| `content_type_changed` | The response stops being JSON. |
| `latency_regression` | Reserved for drift reports; notices SHOULD NOT use it. |

Paths start at `$`, the response body. `.name` selects a property whose name is an
identifier, `["name"]` any other property (as a JSON string), and `[*]` every item of an
array: `$.data[*].customer["e-mail"]`.

Changes that are not about the shape of responses, such as rate limits, authentication or
the meaning of a value, have no kind in version 1. Describe them in `summary` or `note`.

## Signatures

A notice is signed with Ed25519 (RFC 8032) by the provider.

1. **The signed bytes.** Take the notice without its `signature` member and serialize it
   as canonical JSON (RFC 8785, JCS): members sorted by name, no insignificant whitespace,
   UTF-8. Because notices hold only strings, integers, booleans and nulls, and all member
   names are ASCII, any JCS implementation produces the same bytes, and so does
   `json.dumps(notice, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` in Python.
2. **The signature.** Sign those bytes with the provider's private key and put the base64
   (RFC 4648, with padding) of the 64-byte result in `signature.value`, with
   `signature.alg` set to `ed25519`.
3. **The key.** `signature.key` is the URL of the provider's key file, and it MUST be
   exactly `https://<provider>/.well-known/api-change-key`. Pinning the location means no
   other page on the provider's site, such as one that echoes its input, can pass for it.

The key file is plain text with one public key per line, in the style the MCP Registry
uses for domain proofs. Besides key lines it may only hold comments (lines that start
with `#`) and blank lines; a reader MUST reject a file with anything else in it. Lines for
other algorithms (`k=` other than `ed25519`) are skipped. Several keys allow rotation: a
notice verifies when any of them matches.

```text
# Keys for API change notices
v=ACN1; k=ed25519; p=RclZs5zNCbxD+FYntjWS3TvTZ2apD5sGyDCjPFatRdI=
```

`p` is the base64 of the 32-byte raw public key.

To verify a notice, a reader:

1. parses it as JSON, rejecting duplicate member names, and validates it against the
   schema and the rules on this page;
2. checks that `signature.key` is `https://<provider>/.well-known/api-change-key`;
3. downloads the key file, following a redirect only when its target is `https`, on the
   provider's domain and at `/.well-known/api-change-key`, and rejects the file unless it
   holds only key lines, comments and blank lines;
4. computes the signed bytes from the notice as received and checks `signature.value`
   against each key in the file.

## Feeds

A provider lists its notices in a feed, a static JSON file at
`https://<provider>/.well-known/api-changes.json`. A feed MAY also be served from any other
`https` URL, and a feed MAY list notices from several providers: each notice is still
verified against its own provider's key.

```json
{
  "spec": "https://tiltmeter.dev/schemas/api-change-feed/v1.json",
  "notices": [ { "spec": "https://tiltmeter.dev/schemas/api-change-notice/v1.json", "...": "..." } ]
}
```

| Member | Required | Description |
|---|---|---|
| `spec` | yes | Exactly `https://tiltmeter.dev/schemas/api-change-feed/v1.json`. |
| `notices` | yes | Up to 500 full notices, newest first. |

A provider MAY correct a notice in place, keeping its `id`, for editorial fixes. Readers
that already handled the notice will not act on it again, so a change to `endpoints`
belongs in a new notice.

## Reading notices

A reader that acts on notices on its own, such as a scheduled job that opens pull
requests:

- MUST validate every notice on its own, so that one invalid entry never hides the others;
- MUST ignore notices whose signature is missing or does not verify;
- MUST identify a notice by `provider` and `id`, and act on it at most once;
- SHOULD check whether the code it maintains uses what the notice changes before it does
  anything costly, and do nothing when it does not;
- MUST treat `summary`, `note`, `before` and `after` as untrusted data: never as
  instructions to an agent, and never as markup in a pull request or a terminal;
- MUST cap what it downloads, before any decompression, and bound how long a download
  takes, and only fetch over `https`.

A person MAY use an unsigned notice they wrote or trust, from a local file.

## Security considerations

- **Who may speak for an API.** The signature binds a notice to whoever controls the
  provider's host, and every endpoint must be on that host's domain. A notice about
  someone else's API cannot verify. On a shared hosting domain (`github.io`, for
  example), a tenant can only sign as its own subdomain.
- **Replay.** A notice stays valid forever once signed. Readers act on each `provider` and
  `id` once, and `published_at` lets them skip old notices.
- **Untrusted text.** Notices reach coding agents and pull requests. The free text is
  short, plain and free of control characters, and readers must keep it inert.
- **Key compromise.** Remove the key from the key file and publish notices signed with a
  new one. Readers fetch the key file on every run.
