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
    # LIVE-3 (durable provider) — all additive, deny-tightening only:
    account_scope: str | None = None     # exact account fingerprint; NEVER wildcard live
    allowed_commands: frozenset | None = None   # exact command names; None = by risk class
    max_quantity: float | None = None    # largest quantity this grant covers
    adapter_kind: str = "mock"           # the ONLY adapter this grant can apply to

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
            "accountScope": security_config.redact_text(self.account_scope)
                            if self.account_scope else None,
            "allowedCommands": sorted(self.allowed_commands)
                               if self.allowed_commands is not None else None,
            "maxQuantity": self.max_quantity,
            "adapterKind": self.adapter_kind,
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


# ─────────────────────────────────────────────────────────────────────────────
# LIVE-3 — the DURABLE local operator authorization provider.
#
# Replaces the mock-only provider for live use (the mock provider above remains
# available exclusively for the fixture world and tests — it still refuses every
# non-mock adapter). Grants are persisted in the canonical execution store with
# an append-only issuance/revocation audit history; selection is deterministic;
# everything ambiguous denies. Grant records carry NO credential or secret.
# ─────────────────────────────────────────────────────────────────────────────

GRANT_MAX_TTL_SECONDS = 8 * 3600.0        # a grant can never outlive a session day


def _grant_from_row(row: dict) -> AuthorizationGrant:
    import json as _json
    return AuthorizationGrant(
        authorization_id=row["authorization_id"],
        operator_ref=row["operator_ref"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        allowed_risk_classes=frozenset(_json.loads(row["risk_classes_json"])),
        allowed_scopes=frozenset(_json.loads(row["scopes_json"])),
        confirmed=bool(row["confirmed"]),
        reason=row.get("reason", ""),
        revoked=bool(row["revoked"]),
        provider=row.get("provider", "local-operator"),
        account_scope=row.get("account_scope"),
        allowed_commands=(frozenset(_json.loads(row["commands_json"]))
                          if row.get("commands_json") else None),
        max_quantity=row.get("max_quantity"),
        adapter_kind=row.get("adapter_kind") or "mt5",
    )


class DurableOperatorAuthorizationProvider:
    """The real (non-mock) provider: LOCAL-LOOPBACK MT5 activation only.

    Issuance requirements are enforced here, not at the route:
      * explicit confirmation
      * an approved connection profile (local_loopback)
      * an approved, known adapter kind (mt5) — never a different adapter
      * a known account fingerprint scope (NO wildcard account scope for live)
      * bounded lifetime
    Selection (`applicable_grant`) is deterministic: the ACTIVE, scope-exact,
    account-exact, adapter-exact grants sorted by (expires_at, authorization_id)
    — the earliest-expiring applicable grant wins; none applicable -> None.
    """

    provider_name = "local-operator"

    def __init__(self, *, store_fn, active_adapter_kind_fn, account_state_fn,
                 profile_ok_fn, now_iso_fn):
        self._store_fn = store_fn
        self._active_kind = active_adapter_kind_fn
        self._account_state = account_state_fn        # () -> execution_safety.ACCOUNT_*
        self._profile_ok = profile_ok_fn              # () -> bool (approved profile)
        self._now_iso = now_iso_fn

    # ── issuance / revocation ────────────────────────────────────────────────
    def issue(self, *, operator_ref: str, ttl_seconds: float, scopes: set,
              account_scope: str, risk_classes: set, commands: set | None,
              max_quantity: float | None, reason: str, confirmed: bool,
              now: datetime) -> AuthorizationGrant:
        """Issue ONE durable grant. Raises GrantIssuanceError (machine reason)
        when any issuance requirement is unmet. Never stores a secret."""
        store = self._store_fn()
        if store is None:
            raise GrantIssuanceError("execution_store_unavailable")
        if not confirmed:
            raise GrantIssuanceError("confirmation_required")
        if not (isinstance(operator_ref, str) and operator_ref.strip()):
            raise GrantIssuanceError("operator_identity_required")
        if not self._profile_ok():
            raise GrantIssuanceError("connection_profile_not_approved")
        kind = self._active_kind()
        if kind != "mt5":
            raise GrantIssuanceError("adapter_not_grantable")
        import execution_safety as _es
        if self._account_state() != _es.ACCOUNT_MATCH:
            raise GrantIssuanceError("account_identity_not_verified")
        if not (isinstance(account_scope, str) and account_scope.strip()) \
                or account_scope.strip() == SCOPE_ANY:
            raise GrantIssuanceError("account_scope_required")   # no wildcard live scope
        if not scopes or SCOPE_ANY in scopes:
            raise GrantIssuanceError("deployment_scope_required")
        if not risk_classes:
            raise GrantIssuanceError("risk_class_required")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            raise GrantIssuanceError("invalid_ttl")
        if not (0 < ttl <= GRANT_MAX_TTL_SECONDS):
            raise GrantIssuanceError("invalid_ttl")
        if reason is None or not str(reason).strip():
            raise GrantIssuanceError("reason_required")
        issued = now
        expires = datetime.fromtimestamp(now.timestamp() + ttl, timezone.utc)
        grant = AuthorizationGrant(
            authorization_id=new_authorization_id(),
            operator_ref=operator_ref,
            issued_at=issued.isoformat().replace("+00:00", "Z"),
            expires_at=expires.isoformat().replace("+00:00", "Z"),
            allowed_risk_classes=frozenset(str(c) for c in risk_classes),
            allowed_scopes=frozenset(str(s) for s in scopes),
            confirmed=True,
            reason=str(reason),
            provider=self.provider_name,
            account_scope=account_scope.strip(),
            allowed_commands=frozenset(str(c) for c in commands) if commands else None,
            max_quantity=float(max_quantity) if max_quantity is not None else None,
            adapter_kind="mt5",
        )
        store.save_grant({
            "authorization_id": grant.authorization_id,
            "operator_ref": grant.operator_ref,
            "issued_at": grant.issued_at, "expires_at": grant.expires_at,
            "risk_classes": grant.allowed_risk_classes,
            "scopes": grant.allowed_scopes,
            "account_scope": grant.account_scope,
            "commands": grant.allowed_commands,
            "max_quantity": grant.max_quantity,
            "adapter_kind": grant.adapter_kind,
            "confirmed": True, "reason": grant.reason,
            "provider": grant.provider,
        }, now=self._now_iso())
        return grant

    def revoke(self, authorization_id: str, *, operator_ref: str,
               detail: str = "") -> bool:
        store = self._store_fn()
        if store is None:
            raise GrantIssuanceError("execution_store_unavailable")
        return store.revoke_grant(authorization_id, now=self._now_iso(),
                                  operator_ref=operator_ref, detail=detail)

    # ── selection ────────────────────────────────────────────────────────────
    def grants(self) -> list[AuthorizationGrant]:
        store = self._store_fn()
        if store is None:
            return []
        out = []
        for row in store.grants():
            try:
                out.append(_grant_from_row(row))
            except Exception:
                continue                     # malformed rows never authorize
        return out

    def applicable_grant(self, *, now: datetime, command: str | None = None,
                         scope: str | None = None,
                         account_fingerprint: str | None = None,
                         adapter_kind: str | None = None) -> AuthorizationGrant | None:
        """Deterministic selection of the ONE active applicable grant. Exact
        matching, deny by default; the earliest-expiring applicable grant wins
        (smallest remaining authority first, ties by id)."""
        kind = adapter_kind or self._active_kind()
        candidates = []
        for g in self.grants():
            if not g.is_active(now):
                continue
            if g.adapter_kind != kind:
                continue                     # never a different adapter
            if account_fingerprint is not None and g.account_scope != account_fingerprint:
                continue                     # never a different account
            if command is not None and g.allowed_commands is not None \
                    and command not in g.allowed_commands:
                continue
            if scope is not None and SCOPE_ANY not in g.allowed_scopes \
                    and scope not in g.allowed_scopes:
                continue
            candidates.append(g)
        if not candidates:
            return None
        candidates.sort(key=lambda g: (g.expires_at, g.authorization_id))
        return candidates[0]


class GrantIssuanceError(ValueError):
    """Grant issuance/revocation refused. `reason` is a stable machine code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
