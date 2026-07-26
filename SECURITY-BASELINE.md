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
4. **CORS review — RESOLVED in UI-10.** The wildcard-plus-credentials default is
   gone: origins are explicit, validated and loopback-only by default, wildcard is
   unsupported, and credentials are off. See *Browser origin policy* below. Note
   this closes the **browser** boundary only; remote exposure still requires items
   1–3 and 5–7.
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

---

# Browser Origin Policy — CORS (UI-10)

## Threat boundary

| Boundary | Closed by | Status |
| --- | --- | --- |
| A web page the operator visits reading this API with their browser | **CORS (UI-10)** | **Closed** |
| A direct client (curl, script, another process) reaching the API | Network reachability + future authenticated transport | Loopback-only today |
| Anything off this machine | VPN/TLS/auth — UI-11 | Not built |

### What CORS does

Stops a browser on another origin from *reading* responses from this API. That
mattered urgently: before UI-10 the backend answered **every** origin with
`Access-Control-Allow-Origin: *` **and** `Access-Control-Allow-Credentials: true`,
and approved a `DELETE` preflight from an arbitrary remote site. Any page the
operator happened to visit could read the entire Control Tower API — telemetry,
ops status, event log — for as long as the backend was reachable from that browser.

### What CORS does **not** do

- It is **not authentication.** It identifies no one and authorises nothing.
- It is **not a network boundary.** It does nothing about curl, scripts, or any
  client that does not implement it. A request with **no `Origin` header is not a
  CORS failure** — it is simply not a browser request, and it is unaffected.
- It is **not sufficient for VPS exposure.** A remote attacker does not need a
  browser. CORS closes one specific hole and leaves the rest of the security model
  exactly where UI-9 left it.

## Safe local default

With neither variable set, exactly three origins are trusted:

```
http://localhost:3000
http://127.0.0.1:3000
http://[::1]:3000
```

Derived from `frontend/package.json` (`vite --host 0.0.0.0 --port 3000`), not
guessed, and pinned by a test that reads that file.

`localhost`, `127.0.0.1` and `::1` are **distinct origins** to a browser and are
listed separately. Loopback does **not** imply "any port on loopback": trusting
`http://localhost:3000` does not trust `:3001`.

In the ordinary local workflow CORS is not even exercised — with
`REACT_APP_BACKEND_URL` unset, Vite proxies `/api` to the backend server-side, so
requests are same-origin. These origins matter only when the browser is pointed
straight at the backend, which is also why tightening this broke nothing.

## Explicit-origin model

`CORS_ORIGINS` is a comma-separated list of exact origins: `scheme://host[:port]`.
Whitespace trimmed, exact duplicates collapsed, first-seen order preserved.

Rejected, each with a stable issue code: wildcard in any position, `null`,
`file://`, non-http(s) schemes, paths, query strings, fragments, embedded
credentials, invalid ports, unparseable input (including malformed IPv6 literals).
Validation **never raises**.

**Wildcard is unsupported, not merely discouraged** — `*` is rejected as an invalid
entry, which is what makes a credentialed wildcard impossible to configure rather
than something to guard against.

## Credentials policy

`CORS_ALLOW_CREDENTIALS` defaults to **false**, and stays false because the
frontend genuinely does not need it: no cookies, no sessions, no `Authorization`
header, no `credentials: 'include'` anywhere. `allow_credentials=True` was not
preserved for compatibility with behaviour that is not used.

Credentials can only be enabled **alongside an explicit origin list** — enabling
them against the safe default would attach credential semantics to origins the
operator never wrote down. An unrecognised boolean is reported as an issue and
leaves credentials disabled.

## Methods and headers

Evidence-based, from `frontend/src/lib/api.ts`:

- **Methods:** `GET`, `HEAD`, `OPTIONS`, `POST`, `PUT`. `DELETE` and `PATCH` are
  absent — no frontend path uses them.
- **Headers:** `Accept`, `Content-Type`, `Idempotency-Key`.

Permitting `POST`/`PUT` reflects routes the backend **already** serves; UI-10 adds
and exposes no route.

## Invalid-configuration fallback

One deterministic rule: if `CORS_ORIGINS` is set but **no** entry survives
validation, the policy falls back whole to the safe local default, reports
`source: invalid_fallback`, and lists the issue codes. It never falls back to
wildcard and never blocks startup — a CORS typo must not take down local dry-run
work. If *some* entries are valid, those are used and the rejected ones are still
reported.

## Browser preflight behaviour

| Situation | Result |
| --- | --- |
| Allowed origin, allowed method | `200`, `Access-Control-Allow-Origin` echoes the **exact** origin |
| Disallowed origin | `200` server-side, **no** allow-origin header — the browser blocks it |
| No `Origin` header | Normal response; direct clients unaffected |
| Disallowed method (`DELETE`) | Preflight `400` |
| Disallowed header (`Authorization`, `Cookie`) | Preflight `400` |
| Any origin | `Access-Control-Allow-Origin` is never `*`; no `Allow-Credentials` by default |

## Backend bind assumptions

`uvicorn` defaults to host `127.0.0.1`, and no tracked launch command overrides it
(`README.md`: `uvicorn server:app --reload`). The API is therefore **loopback-only
unless an operator passes `--host` deliberately**. UI-10 preserves this and adds no
remote bind option; a test pins both facts.

(The *frontend* dev server does bind `0.0.0.0` via `vite --host`. That serves static
assets, not the API, and is pre-existing — but it means the UI, not the API, is
reachable on the LAN during development.)

## Diagnostics

`GET /api/security/config` gained a `cors` block reporting policy source, origin
**count**, credentials flag, allowed methods/headers, local-only classification,
`wildcardEnabled` (structurally `false`), validation status and issue codes.

**No origin string is ever returned** — not even a loopback one. An origin list is
deployment intelligence, and one uniform rule is easier to keep honest than one
with a local-only exception. The startup log line follows the same rule.

## Activation prerequisites for remote access (unchanged)

CORS hardening does **not** unlock remote access. Items 1–3 and 5–7 of the UI-9
list still apply: authenticated transport, encrypted transport, explicit origin
configuration, explicit bind policy, credential handling and a threat review.

---

# Request Authentication (UI-11) — local contract, **disabled by default**

## Threat boundary

| Boundary | Closed by | Status |
| --- | --- | --- |
| A web page reading the API with the operator's browser | CORS (UI-10) | Closed |
| **Any client — curl, script, another process — reaching the API** | **Bearer authentication (UI-11)** | **Contract defined, disabled by default** |
| Reading the token in transit | TLS | **Not built** |
| Anything off this machine | VPN/TLS/auth together — future | Not built |

Authentication and CORS are different boundaries. CORS restricts *browsers*;
authentication restricts *every* client. Neither substitutes for the other, and
**authentication is not encryption**: a bearer token over plaintext HTTP is
readable by anything on the path. This is why UI-11 is a **local** contract and why
enabling it does **not** authorise remote exposure.

## Enabled / disabled semantics

| Configuration | Result |
| --- | --- |
| Nothing set (default) | **Disabled.** Every route behaves exactly as before. |
| `CONTROL_TOWER_AUTH_ENABLED` explicitly false/blank | Disabled. |
| `CONTROL_TOWER_AUTH_ENABLED=true` + `CONTROL_TOWER_API_TOKEN` set (≥32 chars) | Enforcing. |
| Unrecognised boolean (`ture`, `maybe`) | **Disabled** + issue code. Never enabled by accident. |
| Explicitly true + valid token | Enforcing. |
| Explicitly true + missing/blank/short/whitespace token | **Fails closed** (503). |

The last row is the important one: authentication is **never silently switched back
off** once an operator has explicitly enabled it. Silently reverting to open would
be the worst possible outcome of a typo.

## Token policy

- Fixed scheme and header: `Authorization: Bearer <token>`. A configurable header
  name was considered and rejected — it adds configuration surface and a
  client/server divergence risk for no capability. *(Deviation from the prompt's
  optional `CONTROL_TOWER_AUTH_HEADER`, deliberately.)*
- Minimum length **32 characters**; no internal whitespace (it cannot survive the
  header); blank means **absent**.
- Comparison uses `hmac.compare_digest` — constant time. No hand-rolled equality:
  an early-return `==` leaks the shared prefix length.
- The value lives in a wrapper whose `repr`/`str` are masked and is **absent from
  the policy dataclass**, so there is no field for a future edit to render. It is
  never logged, never hashed into output, never echoed in an error, never returned
  by diagnostics and never sent to the frontend.
- This repository does **not** generate, store or rotate tokens, and ships no
  example value.

## Route classification — deny by default

**Public (exactly one):** `/api/health`.

It is the readiness/bootstrap probe the frontend calls before anything else, and a
liveness probe that requires a credential cannot report that the credential is
misconfigured.

**Protected (everything else),** verified against the live route table: `/api/`
bootstrap metadata, `/api/security/*`, `/api/live/*` (including `ingest`),
`/api/events*`, `/api/ops/status`, `/api/world`, all 10 mutation/command routes,
and `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`.

Docs and OpenAPI are **protected rather than disabled** so local development keeps
them while authentication is off.

Matching is **exact, never prefix-based**: a prefix rule would make a future
`/api/health/secrets` public by inheritance. `OPTIONS` is the only method that
bypasses authentication — a browser cannot attach credentials to a CORS preflight
by specification, and the preflight response carries no data.

### Maintenance rule

A route added tomorrow is protected automatically. A structural test enumerates the
**live app** and asserts every registered path is classified, that every
mutation/command route is protected, and that the public set is exactly
`{/api/health}`. **If you add a route that must be public, you must add it to
`PUBLIC_ROUTES` and the test will make you justify it.** Forgetting the allowlist
can never make something public.

## Unauthorized response contract

One externally indistinguishable response for missing, malformed, wrong-scheme,
empty and incorrect credentials:

```
401  { "error": "unauthorized", "code": "unauthorized", "message": "..." }
     WWW-Authenticate: Bearer
     Cache-Control: no-store
```

Nothing reveals which check failed or how close the credential was — anything else
is an oracle. The credential is never echoed, and no stack trace or request header
appears in the body.

Server-side misconfiguration is different and says so:

```
503  { "error": "unavailable", "code": "authentication_misconfigured", ... }
```

401 would send the caller hunting for a credential they cannot fix.

## CORS interaction

`Authorization` was added to the UI-10 allow-header list so that **if**
authentication is ever enabled, a browser client can preflight it successfully.
This changes nothing else: authentication stays disabled, the frontend sends no
Authorization header, credentials remain disabled, wildcard remains unsupported,
and preflight for `Authorization` still succeeds **only** from an already-trusted
origin (verified from both an allowed and a disallowed origin).

## Logging and redaction

The audit found **no request-header logging anywhere** — only `Idempotency-Key` and
`content-length` are read, neither logged. The existing UI-9 redaction helpers were
**extended** rather than duplicated:

- `Authorization: Bearer <token>` masked in free text, scheme preserved so the log
  still says what kind of credential was involved
- `?token=` / `api_key=` / `password=` / `secret=` / `auth=` masked in query strings
  **and** in bare `name=value` form (exception messages)
- `key`, `session`, `cookie` added to the secret-key hints for mapping redaction
- `redact_text` never raises — a logging call must not be the thing that fails

Authentication failures log the path and nothing about the credential: not its
length, not its prefix, not which check failed.

## Why the frontend has no token storage

Deliberately excluded: no login form, no token input, no `localStorage`,
`sessionStorage` or cookie, no `credentials: 'include'`, no Authorization
injection. A token in browser storage is readable by any script that reaches the
page, and this slice defines the **backend contract** only. The frontend shows
value-free diagnostics and nothing else.

## Local development while disabled

Unchanged. No credential, no header, no configuration: while authentication is
disabled every route responds exactly as it did before this slice, and the full
backend regression suite passes with only the two known environmental collection
errors (`REACT_APP_BACKEND_URL is not set`).

## Future activation prerequisites

Enabling authentication locally is **not** remote authorisation. Before any remote
access:

1. **TLS** — without it the bearer token is plaintext on the wire. Non-negotiable.
2. **Private network path** — VPN or equivalent; the API must never face the
   public internet.
3. **Explicit origin + bind policy** — `CORS_ORIGINS` and an explicit `--host`.
4. **Credential handling** — a real secrets mechanism, not an environment variable
   on one machine; plus rotation, which this slice does not implement.
5. **Threat review** — token scope, revocation, and what a leaked token grants.
6. **Node autonomy preserved** — invariant I-10: the node keeps trading and
   protecting the account when the Control Tower is unreachable.

**Status: authentication preparation is complete at the CONTRACT level only.**
Remote authenticated transport is **not active** and no transport adapter exists.
