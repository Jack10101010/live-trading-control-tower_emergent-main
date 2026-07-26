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

`AuthenticationProvider`, `CertificateProvider` and `ConnectionPolicy` remain
declared-but-unimplemented. `TransportAdapter` has been given its canonical
expansion by **UI-12** (below); `NullTransport` satisfies the original Protocol, so
these references stay valid.

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
Remote authenticated transport is **not active**. The only transport that exists is
`NullTransport` (UI-12), which never communicates; no networking transport exists.

---

# Remote Transport Contract (UI-12) — architecture only

`backend/transport.py` defines the **canonical interface** a future
Control-Tower -> node transport must implement, and ships the default that stands
in until one is built. **It contains no networking and none is reachable from it:**
no socket, no HTTP client, no WebSocket, no polling, no retry, no authentication,
no TLS, no VPS address. A test proves the entire surface can be exercised while any
real socket call is rigged to blow up.

## Why the interface exists before the wire

The Control Tower is observational (**I-7**) and the node stays autonomous when the
tower is unreachable (**I-10**). Remote connectivity is a later, separately-audited
slice gated by the *Activation prerequisites* above. Defining the shape now lets
every future caller depend on **one stable seam** instead of an ad-hoc client, and
lets review see the intended shape before any wire code exists.

## The contract

`Transport` is an abstract base whose operations are all **total and
non-raising** — failures are returned as typed results, never exceptions, so an
observability surface can never be taken down by transport state.

| Operation | Meaning |
| --- | --- |
| `connect()` | Establish the transport → `ConnectionResult` |
| `disconnect()` | Tear down; idempotent |
| `health()` | Can it currently carry traffic? → `TransportResult` (observation only) |
| `request(operation, payload)` | One request/response → `TransportResult` |
| `close()` | Release resources; idempotent, safe after `disconnect()` |
| `describe()` / `status()` | Value-free identification for diagnostics |

Results distinguish `available` (is a transport present at all) from `ok` (did the
operation succeed), so a caller can tell *"no transport"* from *"transport present
but the request failed."* No result field ever carries a credential or a raw wire
payload.

**`stream()` is deliberately omitted.** The node publishes periodic snapshots (a
request/response shape), not a continuous stream, so a streaming operation would be
speculative surface today. It can be added alongside the implementation that
justifies it.

## NullTransport — the default everywhere

`NullTransport` is the default and the only implementation today. Every operation
returns `transport_unavailable` without touching a socket. It is the **correct
behaviour**, not a stub: there is no remote transport, so reporting exactly that is
the truthful answer, and callers always hold a real object with a predictable
result instead of scattering `None` checks.

`default_transport(config=None)` is the **single wiring point**. It returns
`NullTransport` unconditionally in UI-12 (no real transport exists and
`is_active()` is hard-disabled), and accepts `config` so a future slice selects a
concrete transport in exactly one place — never inline. That is what makes
"the default is NullTransport everywhere" a guarantee.

## Future implementations (not built)

A later slice would add, behind this same interface, e.g. an `https` transport
(authenticated request/response over TLS) or a `websocket` transport (if a genuine
streaming need appears). Each must consult `ConnectionPolicy` before opening a
socket, obtain credentials via `AuthenticationProvider`, resolve TLS material via
`CertificateProvider`, and be selected inside `default_transport()`. None of that
exists yet, and UI-12 adds no caller that depends on a concrete transport, so
default behaviour is unchanged.

---

# REST Transport (UI-13) — the first real adapter, **disabled by default**

`backend/rest_transport.py` implements the UI-12 `Transport` contract over HTTP with
bearer authentication. It is the first *real* adapter, and it is off unless
deliberately switched on: **the running Control Tower wires no transport**, and
`default_transport()` returns `NullTransport` in every default configuration.

## Disabled-by-default wiring

`default_transport()` returns `RestTransport` **only** when all of these hold, and
`NullTransport` otherwise (there is no implicit localhost or remote fallback):

1. `CONTROL_TOWER_TRANSPORT_ENABLED` is explicitly truthy (`1/true/yes/on`).
2. The UI-9 security configuration **validates with no errors**.
3. `NODE_TRANSPORT=https` (the only real adapter).
4. `NODE_ENDPOINT` is present.
5. `NODE_API_TOKEN` is present (a bearer transport needs a credential).

This flag is deliberately **separate from UI-9's `is_active()`**, which stays
hard-disabled: `is_active()` is the security-baseline posture indicator, whereas
this flag governs adapter *selection*, and even when set a real transport is chosen
only if the configuration also validates. `rest_transport` is imported lazily, so
no default caller pulls in a networking import.

## Bearer authentication

Every request carries `Authorization: Bearer <token>`. The token is read from the
canonical `NODE_API_TOKEN` (never from `SecurityConfig`, which drops the value by
design), held in a repr-masked `_Secret`, placed only on the outbound header, and
**never** logged, returned in a result, embedded in a URL or query string, or
persisted. No cookies, no sessions. Every response/error string that could reach a
result is passed through the UI-9 `redact_text` masker.

## HTTP safety

Read-only and single-attempt: **GET only**, no POST/PUT/DELETE, no mutation or
command support. Bounded connect/read timeouts, a bounded response-size limit,
`application/json` content-type validation, safe JSON parsing, and a stable reason
map (`timeout`, `connection_error`, `http_status`, `unexpected_content_type`,
`malformed_response`, `response_too_large`, `invalid_operation`). Redirects are
**not followed** (a 3xx surfaces as `http_status`, never a silent cross-host follow
that could carry the token). Failures are **returned as results, never raised**.

**No retries** in this slice. No streaming. No WebSockets.

## HTTP without TLS is local-test-only

Integration tests run against an in-process fake server bound to `127.0.0.1` — never
the VPS, the internet or any external service. A plaintext bearer token is readable
on the wire, so **a real remote endpoint requires TLS**. `RestTransport` accepts an
`http://localhost` endpoint only because `ALLOW_INSECURE_LOCALHOST` permits it for
local testing; it is never a remote posture.

## Remote activation prerequisites (unchanged, still excluded)

Selecting `RestTransport` locally is **not** remote authorisation. Before any remote
use: TLS end to end, a private network path (VPN), an explicit origin/bind policy,
a real secrets mechanism with rotation, retry/backoff design (out of scope here),
and a threat review — the full UI-9 activation list. **This slice adds no VPS
address, no retry logic and opens no remote connection.**

---

# Read-Only Remote Node Integration (UI-14)

`backend/node_client.py` is the first *pull*: the Control Tower reads **health and a
telemetry snapshot FROM the node** through the UI-13 transport, surfaced at
`GET /api/live/remote` and on the operations strip. It is the mirror image of UI-2
(the node PUSHING to `/api/live/ingest`) and is **disabled by default** — with no
operator transport configuration the default transport is `NullTransport`, the
integration reports `disabled`, and no connection is opened.

## Read-only, no command path

The client requests only two read paths — `/health` and `/telemetry` — via GET.
There is **no mutation, no command, no execution control and no acknowledgement**.
The module contains no POST/PUT/DELETE and no order/arm/kill/submit surface.

## State and provenance model

Every result carries provenance **`remote-node`** — remote data is never relabelled
local or fixture, and a failed request is a *state*, never a silent fixture
substitution. States are distinguished, not collapsed:

| State | Meaning |
| --- | --- |
| `disabled` | No real transport selected (the default). Nothing was contacted. |
| `connecting` | Frontend-only, shown while the read is in flight. |
| `healthy` | Reachable, authenticated, telemetry validated **and fresh**. |
| `degraded` | Reachable + authenticated, but telemetry unusable (non-2xx, wrong content-type, malformed, or fails the UI-2 schema). |
| `stale` | Reachable, but `published_at` is old **or implausibly future** (shared UI-2 freshness rule — clock skew is not health). |
| `unauthorized` | The node rejected the credential (401/403). |
| `unreachable` | Timeout or connection error. |

Freshness reuses `live_telemetry.observation`; the payload is validated with the
existing `live_telemetry.validate_snapshot`. **No telemetry model is duplicated** —
the result surfaces only the value-free freshness envelope (instance id, schema
version, `published_at`, age, stale), never the raw snapshot, an endpoint, or a
token.

## Failure behaviour

A failure never falls back to fixtures. `telemetry` is `null` in every non-healthy
state, provenance stays `remote-node`, and the frontend shows the failure state
truthfully (`unauthorized`/`unreachable` are critical, `degraded`/`stale` are
warnings). Errors are redaction-safe: the bearer token lives only inside the
transport, and every surfaced detail passes through `redact_text`.

## Explicit exclusions

No command or execution path, no acknowledgement, no VPS mutation, no service
restart, no retries (single attempt, UI-13), no streaming, no WebSocket. HTTP
without TLS remains **local-test-only**; a real remote endpoint requires TLS and the
other UI-9 activation prerequisites. The default configuration is disabled, and the
running Control Tower opens no remote connection unless an operator deliberately
enables and validly configures one.

# Command Channel Contract (UI-15) — architecture only, sends nothing

`backend/command_channel.py` defines the canonical shape of a **future** operator
command so that when a command path is one day built it is validated, bounded,
auditable, idempotent and redaction-safe **by construction**. UI-15 builds the
*contract and a disabled null implementation only*. Nothing in this slice sends,
dispatches, acknowledges from a node, executes, persists, or reaches the transport
or network. It is the command mirror of UI-12: the interface exists before the wire.

## What is defined

A versioned envelope (`schema_version = "ct.command.v1"`) with immutable, frozen
value objects: `CommandEnvelope` (command id, type, idempotency key, requested-at,
expires-at, optional target and operator reference, bounded JSON payload),
`Acknowledgement`, `Outcome`, and the `CommandRecord` that ties them together with
timestamps. Command ids are collision-resistant (`cmd_` + `uuid4().hex`).

The command **vocabulary is read-only only** — `noop`, `request_health`,
`request_telemetry`. There is deliberately **no** `pause`, `resume`, `arm`,
`close`, `order`, `kill` or any other state-changing verb; an unknown or mutating
type is rejected. This keeps the contract observational (I-7) even in shape.

## Lifecycle and states

Stable, distinguished states — `pending`, `accepted`, `rejected`, `expired`,
`completed`, `failed` — with `rejected`, `expired`, `completed`, `failed` terminal.
The legal transitions are:

```
submit            -> pending
acknowledge(ok)   -> accepted        (pending only)
acknowledge(deny) -> rejected        (pending only, terminal)
record_outcome    -> completed|failed (accepted only)
[lazy, at read]   -> expired         (pending past its expiry window)
```

Any other transition — completing a `pending` command, acknowledging twice,
acknowledging an already-expired or terminal command — is refused with the stable
`invalid_transition` reason code. Rejections and errors always carry a stable
machine reason code (`unknown_command_type`, `expiry_required`, `expiry_invalid`,
`already_expired`, `timestamp_invalid`, `idempotency_required`, `payload_too_large`,
`payload_secret`, `duplicate_command_id`, `channel_disabled`, `invalid_transition`,
`not_found`, …), never a free-text-only failure.

## Acknowledgement vs completion (distinct by design)

**Acknowledgement is not completion.** `acknowledge` records only that the command
was *received and accepted for consideration* (`accepted`) or *refused up front*
(`rejected`). It never implies the command was carried out. **Completion** is a
separate, later transition (`record_outcome` → `completed`/`failed`) that is only
legal from `accepted`. A record therefore carries an `Acknowledgement` and an
`Outcome` as independent fields; a consumer can never mistake "the node heard us"
for "the node did it".

## Idempotency and duplicate handling (deterministic)

An **idempotency key is required** on every envelope. Re-submitting the same key
returns the *same* existing record — a replay, never a second command. Separately,
reusing a **command id** with a *different* idempotency key is a deterministic
`duplicate_command_id` rejection, not a silent overwrite. Both rules are pure and
clock-independent, so retries and at-least-once delivery are safe by contract.

## Payload bounds, JSON safety, and secret rejection

The payload must be a JSON object, must serialise as JSON (`payload_not_json`
otherwise), and is size-bounded (`MAX_PAYLOAD_BYTES`, `payload_too_large` otherwise).
String fields are length-bounded. **Secrets are refused, not stored:** the payload
is recursively scanned with the UI-9 `security_config.is_secret_key` predicate and a
secret-bearing key is rejected with `payload_secret`. Expiry is **required** and
bounded (`MAX_TTL_S`); a future-dated `requested_at` beyond a small clock-skew
tolerance is rejected.

## Audit and redaction-safe diagnostics

`safe_view(record)` produces the **immutable, value-free audit representation**: ids,
type, state, the acknowledged/completed booleans, timestamps, and a payload passed
through `security_config.redact_mapping`. As defence-in-depth the view redacts even a
secret that somehow reached a record, so no diagnostic, log line, or audit entry can
leak a credential.

## Disabled default — no transport, network, execution or persistence

`default_command_channel()` returns a `NullCommandChannel` with `enabled = False`.
Its store is a **bounded in-memory ring** (`MAX_RECENT`) that exists only to make the
contract testable; a fresh channel is empty, nothing is written to disk, and nothing
survives the process. The module imports **no** transport, `rest_transport`,
`node_client`, `socket`, `urllib`, `requests` or `httpx`, calls
`default_transport()` nowhere, and exposes no order/arm/kill surface — enforced by
test. Exercising the entire surface opens no socket (a monkeypatched `socket.socket`
that raises proves it).

## Explicit exclusions

No command is sent, dispatched, acknowledged by a node, or executed. No transport is
touched, no HTTP request made, no node state mutated, no execution/pause/resume/order
control exists, nothing is persisted, and there are **no frontend controls** — this
slice adds no UI. Activating a real command path would be a separate, deliberate,
individually-audited future slice subject to all UI-9 activation prerequisites; until
then the channel is disabled and inert.

# Read-Only Command Transport (UI-16) — disabled by default, sends only reads

`backend/command_transport.py` is the first slice where a command may actually leave
the Control Tower — but only a **read-only** one. It composes the UI-15 command
contract (`command_channel`) with the UI-13 authenticated `Transport` so the three
read-only command types — `noop`, `request_health`, `request_telemetry` — can be
transmitted to the node and their responses mapped back into the UI-15 lifecycle. It
adds **no command model** (UI-15's envelope/acknowledgement/outcome/record are
reused), **no execution authority**, and **no operator control**.

## What it may transmit

Only the UI-15 read-only vocabulary. Each type maps to a bounded GET against a node
read endpoint — and the transport itself is **GET-only** (UI-13), so a mutating
request is not merely disallowed, it is *unrepresentable*:

| Command type | Node read endpoint | Transport call |
| --- | --- | --- |
| `noop` | `/health` | `transport.health()` |
| `request_health` | `/health` | `transport.health()` |
| `request_telemetry` | `/telemetry` | `transport.request("/telemetry")` |

There is no dispatch entry for any other type. `_HEALTH_TYPES ∪ _TELEMETRY_TYPES ==
command_channel.ALLOWED_TYPES` is asserted by test, so the map can never grow a
mutating verb silently.

## Lifecycle mapping (one round-trip, no retry)

A submission is validated and de-duplicated by the UI-15 contract, enrolled as
`pending`, then dispatched **exactly once**. The single `TransportResult` maps onto
the canonical lifecycle:

| Transport result | Acknowledgement | Outcome | Final state |
| --- | --- | --- | --- |
| `ok` (2xx + valid JSON) | accepted | completed | `completed` |
| `http_status` (non-2xx, incl. 401/403) | rejected | — | `rejected` |
| `unexpected_content_type` / `malformed_response` / `response_too_large` / `invalid_operation` | accepted | failed | `failed` |
| `timeout` / `connection_error` / unavailable | — (none) | — | `pending` |

**Acknowledgement is distinct from completion.** A usable answer is *acknowledged*
(the node replied) and then, separately, *completed* (the reply was usable) — two
distinct records with distinct timestamps. A malformed answer is acknowledged yet
**not** completed (its completion fails), which is precisely what proves the two are
not the same thing. An **unreachable node never acknowledged**, so the command is
neither accepted, rejected nor completed — it stays `pending` and lazily expires.
Nothing is ever fabricated as success.

## Enforced properties

- **Disabled by default.** `default_command_transport()` uses
  `transport.default_transport()`, which is `NullTransport` unless an operator has
  explicitly enabled and validly configured a real transport (UI-13). A disabled
  service records for audit but **transmits nothing** (`command_transport_disabled`).
- **Read-only allowlist**, enforced twice — by `validate_envelope` and again before
  dispatch. Unknown/mutating types are rejected and never transmitted.
- **Expiry before send** (a command already expired is rejected; nothing is sent),
  **idempotency key required**, **deterministic duplicate handling** (idempotent
  replay returns the same record and is *not* re-sent; a colliding command id is a
  deterministic rejection), **no secret-bearing payloads**, **no retry**
  (one attempt), **redaction-safe failures** (`redact_text` on every surfaced detail).
- **No new API route.** This slice adds none — the service is a backend module,
  wired-ready but not exposed. Should a future slice add a route, it must be
  authenticated by UI-11, remain backend-only, expose no frontend control, and reject
  mutating types. The bearer credential lives only inside the UI-13 transport.

## Activation rules

The service is inert until an operator *deliberately* enables the underlying
transport: `CONTROL_TOWER_TRANSPORT_ENABLED` truthy **and** a valid
`NODE_TRANSPORT=https` endpoint + `NODE_API_TOKEN` that pass UI-9 validation. Even
then it can only issue read-only GETs. HTTP without TLS remains local-test-only; a
real remote endpoint requires TLS and the other UI-9 prerequisites. Every network
test runs against an in-process fake transport or a loopback server on `127.0.0.1`.

## Explicit exclusions

No `pause`, `resume`, `arm`, `order`, `cancel` or `kill` command exists or can be
expressed. No mutation, no execution path, no VPS mutation, no node-state change, no
streaming, no WebSocket, no cookies, no sessions, no retries. No frontend changes and
no operator controls. The default remains disabled, and the running Control Tower
transmits no command unless an operator deliberately enables and validly configures a
real transport — and even then, only reads.

# Operator-Facing Read-Only Command Flow (UI-17) — authenticated routes + a UI panel

UI-17 is the first slice where the operator can *originate* a command from the
Control Tower. It exposes the UI-16 read-only transport through authenticated backend
routes and a small frontend panel. Only the three read-only command types —
`noop`, `request_health`, `request_telemetry` — are representable end to end. There
is no execution, arming, order, or node-state change anywhere in the flow, and the
whole surface is disabled and inert by default.

## Backend routes (deny-by-default protected, UI-11)

A single disabled service instance (`server._COMMAND_TRANSPORT =
command_transport.default_command_transport()`) backs three routes under a **distinct**
namespace — never the fixture-world `/api/commands/{name}` execution path, the
orchestrator, or any BotEvent write:

| Method + path | Purpose |
| --- | --- |
| `POST /api/operator/commands` | Submit one permitted read-only command |
| `GET /api/operator/commands/{command_id}` | Query one command's lifecycle status |
| `GET /api/operator/commands` | List recent command records (newest-first, bounded) |

All three are **protected by the UI-11 boundary**: they are not in the one-entry
`PUBLIC_ROUTES` allowlist, so when authentication is enabled every call requires
`Authorization: Bearer <token>` and an unauthenticated or wrong-token request gets the
single stable `401` — verified by test. The server never reuses `KNOWN_COMMANDS`, the
execution orchestrator, or the event store; a submit appends **no** BotEvent.

Submission builds a full UI-15 envelope from a minimal body
(`{commandType, idempotencyKey, ttlSeconds?, operatorRef?, payload?}`): the server
stamps `requested_at = now` and a **bounded, always-present** `expires_at`
(`now + ttl`, default 30 s, capped), so **expiry is required** by construction.
**Idempotency is required and client-owned** — a missing `idempotencyKey` is rejected,
not silently generated. The envelope goes to `CommandTransportService.submit`, which
strictly validates (read-only allowlist, no secrets, JSON-bounded payload),
de-duplicates, and dispatches at most once (no retry). A contract violation is a
**stable, redaction-safe `422`** carrying a fixed machine `code` (e.g.
`unknown_command_type`, `idempotency_key_required`, `already_expired`,
`payload_contains_secret`) and a `redact_text`-scrubbed detail — never a `500`, never a
value. Responses are the value-free `command_channel.safe_view` plus transport-step
metadata and an `enabled` flag; the bearer token never appears in any response.

## Lifecycle mapping surfaced to the operator

The routes surface exactly the UI-15 lifecycle the UI-16 service produces: `pending`,
`accepted`, `rejected`, `expired`, `completed`, `failed`, with **acknowledgement
distinct from completion**. A **disabled** transport records the command and returns
`pending` with `transport.reason = command_transport_disabled` and `enabled: false`
— truthful, never a fabricated success. An **unreachable** node returns `pending`
(`node_timeout` / `node_unreachable`); a **remote refusal** returns `rejected`; a
**usable answer** returns `completed`.

## Frontend panel (read-only, no optimistic success)

`OperatorCommandPanel` (System view) offers exactly three buttons — request health,
request telemetry, and an optional no-op diagnostic. It honours:

- **No optimistic success.** Nothing is shown as accepted/completed until the backend
  returns the mapped lifecycle state; a disabled/unreachable transport is shown as
  exactly that.
- **No duplicate submissions.** Every button is disabled and `aria-busy` while a
  submission is in flight, so a double-click cannot fire two actions.
- **A fresh idempotency key per intentional action** (generated in the API layer), so
  a genuine retry de-duplicates while a new action is distinct.
- **No token handling in the browser.** The panel sends no credential and stores none;
  the payload never contains one (secrets are rejected server-side).
- **No execution controls** — the only representable actions are the three read-only
  types.
- **Accessible loading and error states** — `role=status` live region for progress and
  results, `role=alert` for a rejection (with its stable code) or a failure.

## Activation rules

The routes exist and are protected regardless of state, but they can only *do*
anything once an operator deliberately enables the underlying transport
(`CONTROL_TOWER_TRANSPORT_ENABLED` + a valid `NODE_TRANSPORT=https` endpoint +
`NODE_API_TOKEN`, all passing UI-9 validation) — and authentication should be enabled
(`CONTROL_TOWER_AUTH_ENABLED` + a valid token) before any non-local exposure. Until
then the panel and routes are live but inert: every submission is recorded as
`pending` / disabled and nothing leaves the tower.

## Explicit exclusions

Only health, telemetry and no-op are representable. No trading, no order/cancel, no
pause/resume/arm/kill, no node or VPS mutation, no execution path, no BotEvent, no
reuse of the fixture command route, no retries, no token in the browser, and no
optimistic success. The default is disabled, and the panel truthfully reports it.
