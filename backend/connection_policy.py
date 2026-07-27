"""ARCH-3 — the canonical, fail-closed ConnectionPolicy.

UI-9 declared `security_config.ConnectionPolicy` as "the fail-closed gate a future
slice must consult before any socket is opened" and Audit A found it was never
implemented — UI-13 opened sockets without it. This module is that gate.

ONE policy, consulted by `rest_transport` IMMEDIATELY before every outbound socket
operation (there is no other outbound socket path in the tower). It answers with an
immutable decision carrying a machine-readable reason code and a redaction-safe
audit view. DENY BY DEFAULT: unknown profiles deny, unknown schemes deny, policy
evaluation errors deny, remote anything denies until its prerequisites exist.

SELECTION vs CONNECTION are separate decisions:
  * `transport.default_transport()` decides WHICH transport object exists
    (enable flag + valid configuration). That is selection.
  * THIS policy decides whether a specific outbound connection may be attempted at
    the moment it is attempted. A transport object existing never implies its
    connections are approved.

OPERATING PROFILES
    local_loopback   — the ONLY approved profile. Loopback hosts only; plain HTTP
                       tolerated for local development; HTTPS also fine.
    remote_pre_live  — DENIES. Exists so the missing prerequisites are named
                       (TLS trust, VPN/private path, secrets management,
                       explicit activation attestation) instead of silently
                       unreachable. Nothing here fakes their completion.
    remote_live      — DENIES unconditionally in this repository state.

No secrets are read here; only presence/validity of configuration is consulted.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import security_config

# ── profiles ──────────────────────────────────────────────────────────────────
PROFILE_LOCAL_LOOPBACK = "local_loopback"
PROFILE_REMOTE_PRE_LIVE = "remote_pre_live"
PROFILE_REMOTE_LIVE = "remote_live"
KNOWN_PROFILES = frozenset({PROFILE_LOCAL_LOOPBACK, PROFILE_REMOTE_PRE_LIVE,
                            PROFILE_REMOTE_LIVE})

#: The only profile approved in this repository state.
APPROVED_PROFILES = frozenset({PROFILE_LOCAL_LOOPBACK})

VAR_PROFILE = "CONTROL_TOWER_CONNECTION_PROFILE"

# ── decision reason codes (stable, machine-readable, value-free) ──────────────
ALLOW_LOCAL_LOOPBACK = "allow_local_loopback"
DENY_PROFILE_UNKNOWN = "profile_unknown"
DENY_PROFILE_NOT_APPROVED = "profile_not_approved"
DENY_TRANSPORT_DISABLED = "transport_disabled"
DENY_CONFIG_INVALID = "configuration_invalid"
DENY_ENDPOINT_INVALID = "endpoint_invalid"
DENY_SCHEME_NOT_ALLOWED = "scheme_not_allowed"
DENY_REMOTE_PLAIN_HTTP = "remote_plain_http"
DENY_HOST_NOT_LOOPBACK = "host_not_loopback_for_local_profile"
DENY_HOST_NOT_APPROVED = "host_not_in_approved_list"
DENY_AUTH_NOT_CONFIGURED = "authentication_not_configured"
DENY_TLS_TRUST_MISSING = "tls_trust_not_configured"
DENY_POLICY_ERROR = "policy_evaluation_error"

#: Prerequisites remote profiles would need. NONE of these can be satisfied in
#: this repository state — they are named, not faked.
REMOTE_PREREQUISITES = (
    "tls_trust_configured",        # CA bundle / pin present and valid
    "private_network_attested",    # VPN or equivalent private path
    "secrets_management",          # a real secrets mechanism, not bare env vars
    "remote_activation_attested",  # an explicit, audited operator approval
)

_LOOPBACK_NAMES = frozenset({"localhost"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class PolicyDecision:
    """Immutable outcome of one connection-policy evaluation."""
    allowed: bool
    reason: str
    profile: str
    scheme: str | None = None
    host_class: str | None = None      # "loopback" | "remote" | None — never the host
    port: int | None = None
    detail: str = ""
    missing_prerequisites: tuple = field(default_factory=tuple)

    def safe_view(self) -> dict:
        """Redaction-safe audit view: classifications only — no full URL, no host
        name (an endpoint host is deployment intelligence), no credential."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "profile": self.profile,
            "scheme": self.scheme,
            "hostClass": self.host_class,
            "port": self.port,
            "detail": security_config.redact_text(self.detail) if self.detail else "",
            "missingPrerequisites": list(self.missing_prerequisites),
        }


def _is_loopback_host(host: str) -> bool:
    if not host:
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def active_profile(env: dict | None = None) -> str:
    import os
    source = os.environ if env is None else env
    raw = source.get(VAR_PROFILE)
    if raw is None or not str(raw).strip():
        return PROFILE_LOCAL_LOOPBACK
    return str(raw).strip()


def _missing_remote_prerequisites(source: dict) -> tuple:
    """Which remote prerequisites are absent. In this repository state the
    private-network, secrets-management and activation attestations have no
    satisfying mechanism at all, so they are ALWAYS missing — deliberately."""
    missing = []
    ca = source.get(security_config.VAR_NODE_CA_PATH)
    if not (isinstance(ca, str) and ca.strip()):
        missing.append("tls_trust_configured")
    # No mechanism exists to attest these yet; they cannot be satisfied.
    missing.extend(["private_network_attested", "secrets_management",
                    "remote_activation_attested"])
    return tuple(missing)


def evaluate(url: str, *, env: dict | None = None,
             token_present: bool | None = None) -> PolicyDecision:
    """Evaluate whether ONE outbound connection may be attempted right now.

    `env` — the environment to judge (None = process environment). When `env` is
    None and the caller is an already-selected transport, enablement/config
    validity were enforced at selection; they are re-checked whenever an env is
    supplied. `token_present` lets the transport assert structurally that it holds
    a credential (the policy never sees the value).

    NEVER raises: any internal error returns a DENY (`policy_evaluation_error`).
    """
    import os
    source = os.environ if env is None else env
    profile = active_profile(source)
    try:
        # 1) Profile must be known; only approved profiles may connect at all.
        if profile not in KNOWN_PROFILES:
            return PolicyDecision(False, DENY_PROFILE_UNKNOWN, profile,
                                  detail=f"unknown profile {profile!r}")
        if profile not in APPROVED_PROFILES:
            return PolicyDecision(
                False, DENY_PROFILE_NOT_APPROVED, profile,
                detail="remote profiles deny until their prerequisites exist",
                missing_prerequisites=_missing_remote_prerequisites(source))

        # 2) When judging a full environment: enablement + configuration validity.
        if env is not None:
            raw = source.get("CONTROL_TOWER_TRANSPORT_ENABLED", "")
            if not (isinstance(raw, str) and raw.strip().lower() in _TRUTHY):
                return PolicyDecision(False, DENY_TRANSPORT_DISABLED, profile)
            cfg = security_config.load_config(source)
            if security_config.has_errors(security_config.validate_config(cfg)):
                return PolicyDecision(False, DENY_CONFIG_INVALID, profile,
                                      detail="security configuration has errors")

        # 3) Endpoint anatomy.
        parts = urlsplit(url or "")
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or ""
        port = parts.port
        if not scheme or not host:
            return PolicyDecision(False, DENY_ENDPOINT_INVALID, profile,
                                  detail="endpoint missing scheme or host")
        if scheme not in ("http", "https"):
            return PolicyDecision(False, DENY_SCHEME_NOT_ALLOWED, profile,
                                  scheme=scheme, detail=f"scheme {scheme!r}")
        loopback = _is_loopback_host(host)
        host_class = "loopback" if loopback else "remote"

        # 4) Authentication must be configured (presence only, never the value).
        if token_present is None:
            token = source.get(security_config.VAR_NODE_API_TOKEN)
            token_present = isinstance(token, str) and bool(token.strip())
        if not token_present:
            return PolicyDecision(False, DENY_AUTH_NOT_CONFIGURED, profile,
                                  scheme=scheme, host_class=host_class, port=port)

        # 5) Profile rules. local_loopback: loopback hosts ONLY; plain HTTP is
        #    tolerated for local development; HTTPS also allowed.
        if not loopback:
            # A remote host under the local profile is a hard deny — and remote
            # plain HTTP is called out with its own reason so the operator sees
            # exactly what was wrong even once remote profiles exist.
            if scheme == "http":
                return PolicyDecision(False, DENY_REMOTE_PLAIN_HTTP, profile,
                                      scheme=scheme, host_class=host_class, port=port)
            return PolicyDecision(False, DENY_HOST_NOT_LOOPBACK, profile,
                                  scheme=scheme, host_class=host_class, port=port)

        return PolicyDecision(True, ALLOW_LOCAL_LOOPBACK, profile,
                              scheme=scheme, host_class=host_class, port=port)
    except Exception as exc:               # FAIL CLOSED — an error is a deny
        return PolicyDecision(False, DENY_POLICY_ERROR, profile,
                              detail=type(exc).__name__)


ALLOW_LOCAL_BROKER = "allow_local_broker"
DENY_ADAPTER_UNKNOWN = "adapter_unknown"


def evaluate_local_broker(kind: str, env: dict | None = None) -> PolicyDecision:
    """LIVE-1: may a LOCAL broker terminal adapter (e.g. the MT5 terminal on this
    host) be constructed and queried? Deny-by-default: unknown kinds deny, unknown
    profiles deny, and only the approved local_loopback profile permits local
    terminal access. Remote profiles keep denying with their named prerequisites.
    Never raises."""
    import os
    source = os.environ if env is None else env
    profile = active_profile(source)
    try:
        if profile not in KNOWN_PROFILES:
            return PolicyDecision(False, DENY_PROFILE_UNKNOWN, profile,
                                  detail=f"unknown profile {profile!r}")
        if profile not in APPROVED_PROFILES:
            return PolicyDecision(False, DENY_PROFILE_NOT_APPROVED, profile,
                                  missing_prerequisites=_missing_remote_prerequisites(source))
        if kind not in ("mock", "mt5"):
            return PolicyDecision(False, DENY_ADAPTER_UNKNOWN, profile,
                                  detail=f"unknown adapter kind {kind!r}")
        return PolicyDecision(True, ALLOW_LOCAL_BROKER, profile,
                              host_class="local-terminal")
    except Exception as exc:               # FAIL CLOSED
        return PolicyDecision(False, DENY_POLICY_ERROR, profile,
                              detail=type(exc).__name__)


class CanonicalConnectionPolicy:
    """Satisfies the UI-9 `security_config.ConnectionPolicy` Protocol
    (`may_connect() -> bool`) for a fixed URL + environment."""

    def __init__(self, url: str, env: dict | None = None):
        self._url = url
        self._env = env

    def may_connect(self) -> bool:
        return evaluate(self._url, env=self._env).allowed
