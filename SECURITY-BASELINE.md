# Security Baseline (UI-9) — **NOT ACTIVE**

> **UI-9 introduces no connectivity.** There is no transport, no authentication, no
> TLS, no VPN, no tunnel, no WebSocket and no reverse proxy in this build. Nothing
> here contacts the VPS, and no environment variable can make it do so.

This slice prepares the *architecture* for eventual secure connectivity. It
declares configuration, validates it, guarantees secrets cannot be logged, and
names the seams a future slice must implement. It implements none of them.

## Why "declared but unused" is the point

The dangerous version of this work is a module that quietly becomes live when
someone sets an environment variable. So:

- `security_config.is_active()` is **hard-wired `False`**, not derived from
  configuration. No combination of variables returns `True`
  (`test_no_default_enables_connectivity` asserts this with every variable set).
- No code path consumes these values to open a connection. There is no client to
  configure — `security_config.py` contains no socket, no `ssl`, no `urllib`, no
  HTTP library, asserted structurally by `test_module_opens_no_connection`.
- Enabling connectivity is a later, separately-audited slice that must **add an
  adapter**. It is not a config flip.

## Configuration ownership

One module owns every security variable: `backend/security_config.py`. One reader,
one parse, one validation pass.

This matters because the audit for this slice found `MARKET_DATA_DIR` parsed in two
places (`backend/server.py` and `backend/data_service.py`) with different
fallbacks. Security configuration must never acquire that property — divergent
parsing of a credential or an endpoint is how a system ends up talking to
somewhere nobody intended.

Rules:

- **Missing stays missing.** A blank value means *absent*, never "empty but set".
- **Nothing invents an endpoint.** No `localhost` default, no implicit port.
- **An unrecognised boolean is not silently `False`.** `NODE_TLS_ENABLED=ture` is
  reported as invalid rather than read as "off", because silently reading a typo as
  disabled hides operator intent.

## The variables

All are **blank** in `backend/.env.example` and unset in every environment.

| Variable | Purpose (future) | Notes |
| --- | --- | --- |
| `CONTROL_TOWER_MODE` | Tower operating mode | Only `observe` is supported |
| `NODE_ENDPOINT` | Where a future adapter *would* reach the node | Never contacted. `https` required for any non-local host |
| `NODE_TRANSPORT` | Which adapter to select | `none` \| `https` \| `mtls` — none implemented |
| `NODE_TLS_ENABLED` | TLS posture | Contradicting the endpoint scheme is an error |
| `NODE_CERT_PATH` | Server certificate | Path only |
| `NODE_CA_PATH` | CA bundle for verification | Path only |
| `NODE_CLIENT_CERT` | Client certificate (mTLS) | Required *with* the key |
| `NODE_CLIENT_KEY` | Client private key (mTLS) | **Secret.** Path only, never read |
| `NODE_API_TOKEN` | Bearer credential for a future transport | **Secret.** Only presence is recorded |
| `NODE_CONNECT_TIMEOUT` | Connect timeout, seconds | Validated 0.1–300; no client uses it |
| `NODE_READ_TIMEOUT` | Read timeout, seconds | Validated 0.1–300; no client uses it |
| `ALLOW_INSECURE_LOCALHOST` | Local-development escape hatch | Always warns; never production |

## Validation

`validate_config()` returns findings and **never raises or exits**. Every setting is
future-only, so a problem here cannot break today's dry-run work — a warning that
gets read beats a startup failure that blocks the work in progress. A future slice
that actually connects **must treat `error` findings as fatal before connecting**.

Checked:

- **Malformed URLs** — not absolute, missing scheme/host, unsupported scheme,
  unparseable (including an unclosed IPv6 literal, which makes `urlparse` itself
  raise — guarded, and the reason the `_safe_urlparse` helper exists).
- **Credentials in the endpoint** — `scheme://user:pass@host` is an error.
  Credentials belong in a credential variable, never in a URL.
- **Plaintext transport** — `http` to a remote host is an error; `http` to
  localhost warns unless `ALLOW_INSECURE_LOCALHOST` is explicitly set.
- **Invalid timeouts** — unparseable, non-finite, zero, negative, out of range.
- **Conflicting options** — TLS disabled while TLS material is configured; TLS
  enabled against an `http` endpoint; a client certificate without its key (or
  vice versa); `mtls` without a client identity.
- **Impossible / dead configuration** — `NODE_TRANSPORT=none` with an endpoint set;
  a token with no endpoint to send it to. Both warn: they are wasted intent, not
  faults.

Findings are deterministically ordered, errors first.

## Secret handling

**No secret is ever committed.** `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`,
`credentials.json` were already ignored; UI-9 additionally ignores `*.crt`, `*.cer`,
`*.csr`, `*.p12`, `*.pfx`, `*.jks`, `secrets/` and `certs/`. Only
`.env.example` templates are tracked, and they contain no values.

**The token value is never retained.** `load_config()` records
`api_token_present: bool` and drops the value on the floor, so it cannot be logged,
serialized or returned by an API even by accident.

**Redaction helpers**, applied at the two leak channels the audit actually found:

| Channel | Risk | Fix |
| --- | --- | --- |
| `backend/server.py` Mongo failure log | A driver exception can echo the connection URI, and a Mongo URI embeds `user:pass@host` | `redact_text(exc)` |
| `backend/server.py` command log line | Logged the **whole command payload**; no command carries a credential today, but any future one would have been logged verbatim | `redact_mapping(payload)` |

`redact_mapping` masks values whose key matches a broad hint list (`password`,
`token`, `authorization`, `secret`, `credential`, `private`, `nonce`, `digest`, …)
while preserving structure, so log lines stay diagnosable. It is depth-bounded so a
pathological structure cannot hang a logging call. The hint list is deliberately
over-inclusive: a false positive costs one masked log line, a false negative leaks
a credential into a log file that may be shipped elsewhere.

## Read-only diagnostics

`GET /api/security/config` returns **status only** — per variable
`configured` / `missing` / `invalid`, plus findings and `active: false`. It never
returns a value, not even a non-secret one: an endpoint hostname or certificate path
is still deployment intelligence. The System view renders this as a
`NOT ACTIVE` panel with no control of any kind.

## Future transport roadmap

Four Protocols are **declared, not implemented**, in `security_config.py`:

| Interface | Responsibility |
| --- | --- |
| `TransportAdapter` | The wire. Keeps the tower uncoupled from any one protocol, mirroring the existing MT5-behind-an-adapter rule |
| `AuthenticationProvider` | Supplies credentials to a transport without exposing them to callers |
| `CertificateProvider` | Resolves TLS material; paths only, contents never cross the boundary |
| `ConnectionPolicy` | The fail-closed gate a future slice must consult **before** any socket opens |

Nothing implements or instantiates them, and nothing calls them.

## Activation prerequisites

Before any connectivity slice (UI-11) may proceed:

1. **Network path** — a private link (VPN or equivalent). Not in scope here; the
   node must remain reachable only over a trusted path, never the public internet.
2. **TLS** — `https` end to end, with the node presenting a verifiable certificate
   and the tower verifying it against a pinned CA.
3. **Authentication** — mutual, with credentials supplied through
   `AuthenticationProvider` and never placed in a URL.
4. **CORS review** — `CORS_ORIGINS` currently defaults to `*` alongside
   `allow_credentials=True` in `backend/server.py`. UI-9 deliberately did **not**
   change it (CORS hardening is explicitly out of scope for this slice), but a
   permissive wildcard must be replaced with an explicit origin list before the
   backend is reachable from anywhere but localhost. **This is a blocking
   prerequisite, recorded here so it is not forgotten.**
5. **Secrets at rest** — a real secrets mechanism. Environment variables on a
   single operator machine are acceptable for local development only.
6. **Fail-closed gate** — `ConnectionPolicy` implemented and consulted before the
   first socket, with `error` findings from `validate_config()` treated as fatal.
7. **Direction of trust** — the node stays authoritative and autonomous. Any
   transport must preserve invariant **I-10**: the node keeps trading and
   protecting the account when the Control Tower is unreachable.

## What UI-9 did not touch

No execution, node, telemetry, arming or reconciliation behaviour. No command
route. No HTTP client, retry, polling or WebSocket. No certificate or secret
generation. No deployment, proxy, container or infrastructure change. The frontend
remains read-only.
