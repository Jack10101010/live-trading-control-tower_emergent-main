"""ARCH-3 — tower-owned operator COMMAND AUTHORIZATION. Not node arming.

The node owns live arming (session, nonce, account fingerprint, broker-side
safety — invariant I-7). The tower owns a different question: *has an operator
explicitly authorized this class of command, for this scope, right now?* This
module models that question precisely, replacing the generic "armed boolean" the
mock context used.

`AuthorizationGrant` is immutable, bounded in lifetime, deny-by-default and
scope-exact. A grant can never override node health, account identity,
reconciliation posture or broker state — those gates run independently in
`execution_safety` and the execution context; a grant only ever ADDS the
tower-side authorization fact.

THE ONLY PROVIDER IN THIS SLICE IS THE MOCK PROVIDER: explicitly labelled, only
willing to authorize against the MOCK adapter, deny-by-default everywhere else.
There is no public route that issues, mutates or revokes grants; issuing real
grants is Live-1 work behind its own audit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import security_config

AUTHORIZATION_PREFIX = "auth_"

#: Scope wildcard. Deliberately supported ONLY for the mock provider's fixture
#: world (where deployment ids are fixture data); a real provider must issue
#: exact scopes. The wildcard's presence is visible in every audit view.
SCOPE_ANY = "*"


def new_authorization_id() -> str:
    return f"{AUTHORIZATION_PREFIX}{uuid.uuid4().hex}"


def _parse(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class AuthorizationGrant:
    """One immutable operator authorization. Deny-by-default semantics: a grant
    authorizes ONLY the exact risk classes and scopes it names, only between
    `issued_at` and `expires_at`, only while unrevoked and confirmed."""
    authorization_id: str
    operator_ref: str
    issued_at: str
    expires_at: str
    allowed_risk_classes: frozenset      # e.g. {"execution_affecting"}
    allowed_scopes: frozenset            # deployment/account ids, or SCOPE_ANY (mock)
    confirmed: bool = False
    reason: str = ""
    revoked: bool = False
    provider: str = "unspecified"        # who issued it — "mock" is explicit

    def is_active(self, now: datetime) -> bool:
        """Active = confirmed, unrevoked, inside its bounded lifetime. Any parse
        failure answers False — revocation and malformed data fail closed."""
        if self.revoked or not self.confirmed:
            return False
        issued, expires = _parse(self.issued_at), _parse(self.expires_at)
        if issued is None or expires is None:
            return False
        return issued <= now < expires

    def authorizes(self, *, risk_class: str, scope: str | None, now: datetime) -> bool:
        """Exact matching, deny by default: the risk class must be named, and the
        scope must be named (or the grant carries the explicit mock wildcard)."""
        if not self.is_active(now):
            return False
        if risk_class not in self.allowed_risk_classes:
            return False
        if SCOPE_ANY in self.allowed_scopes:
            return True
        return scope is not None and scope in self.allowed_scopes

    def safe_view(self) -> dict:
        """Redaction-safe audit representation: the operator reference is masked
        (identity is proven by the grant, not displayed)."""
        return {
            "authorizationId": self.authorization_id,
            "operator": security_config.redact_text(self.operator_ref),
            "issuedAt": self.issued_at,
            "expiresAt": self.expires_at,
            "allowedRiskClasses": sorted(self.allowed_risk_classes),
            "allowedScopes": sorted(self.allowed_scopes),
            "confirmed": self.confirmed,
            "revoked": self.revoked,
            "reason": self.reason,
            "provider": self.provider,
            "wildcardScope": SCOPE_ANY in self.allowed_scopes,
        }


def revoke(grant: AuthorizationGrant) -> AuthorizationGrant:
    """Revocation produces a NEW immutable grant record with `revoked=True`; the
    original object is untouched (grants are immutable). A revoked grant never
    authorizes anything again — `is_active` fails closed on the flag."""
    from dataclasses import replace as _replace
    return _replace(grant, revoked=True)


class MockAuthorizationProvider:
    """EXPLICITLY MOCK. Issues a fixture-world grant so the mock control plane
    keeps working — and refuses to authorize anything for a non-mock adapter, so
    this provider can never survive adapter activation.

    Outside the mock adapter (or when disabled) every request denies."""

    provider_name = "mock"

    def __init__(self, *, active_adapter_kind_fn, operator_ref_fn,
                 ttl_seconds: float = 3600.0):
        self._active_kind = active_adapter_kind_fn
        self._operator_ref = operator_ref_fn
        self._ttl = float(ttl_seconds)

    def current_grant(self, now: datetime) -> AuthorizationGrant | None:
        """The mock grant — ONLY while the active adapter is the mock. Any other
        adapter kind gets None (deny-by-default), structurally guaranteeing the
        permissive fixture authorization cannot leak into a live configuration."""
        if self._active_kind() != "mock":
            return None
        # The fixture grant is a STANDING authorization: issued from a short
        # backdated anchor so a command whose request timestamp was captured just
        # before context assembly still falls inside the window. (A real provider
        # issues at the operator's explicit action time; this backdate is a mock
        # convenience only.)
        issued = datetime.fromtimestamp(now.timestamp() - 60.0, timezone.utc)
        expires = datetime.fromtimestamp(now.timestamp() + self._ttl, timezone.utc)
        return AuthorizationGrant(
            authorization_id=new_authorization_id(),
            operator_ref=self._operator_ref(),
            issued_at=issued.isoformat().replace("+00:00", "Z"),
            expires_at=expires.isoformat().replace("+00:00", "Z"),
            allowed_risk_classes=frozenset({"operational", "execution_affecting",
                                            "emergency"}),
            allowed_scopes=frozenset({SCOPE_ANY}),   # fixture world; explicit + visible
            confirmed=True,
            reason="mock fixture control-plane authorization",
            provider=self.provider_name,
        )
