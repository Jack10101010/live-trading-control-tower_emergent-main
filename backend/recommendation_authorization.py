"""LIVE-4E — the smallest possible permission gate for operator decisions.

WHY THIS EXISTS, AND WHY IT IS SO SMALL
    The audit (PART 1/6) found no permission model to reuse:

      * `auth_policy` is authentication, and says so itself — "NOT identity.
        There are no users, roles or sessions — one shared token". Holding the
        token proves the caller has the secret, NOT who they are.
      * It is DISABLED by default, so on a default deployment every route is
        open.
      * `server._operator_id()` returns the first operator in the fixture WORLD,
        or the literal "system". It is a display value, not an authenticated
        principal.

    So there is nothing to reuse for AUTHORIZATION, and inventing a user/role/
    session system is explicitly out of scope. This module therefore does the
    least thing that satisfies "no public anonymous mutation":

      1. It REUSES `auth_policy` for authentication — it never re-implements it,
         never reads the token, and never sees credential material.
      2. It requires an EXPLICIT operator identity on every decision. A request
         that asserts no identity is refused, whether or not the global auth
         gate is switched on. Anonymous mutation is impossible by construction.
      3. It records how much the identity is actually worth. When the API
         boundary authenticated the request the decision is stamped
         `authenticated`; when the boundary is off it is stamped `asserted`.
         The audit trail therefore never claims assurance the system does not
         have — this is the honest alternative to pretending a shared token
         identifies a person.

    Point 3 is the important one. It would be easy to write "actor: op_jane" in
    the ledger and let a reader assume Jane was authenticated. She was not. The
    distinction is recorded on every decision and rendered in the UI.

WHAT THIS MODULE IS NOT
    Not an authentication system, not a session store, not a role model, not an
    execution authorization (`command_authorization` remains the sole authority
    for that, untouched). Granting an operator the right to DECIDE grants no
    right to execute anything.
"""

from __future__ import annotations

import re

import auth_policy
import recommendation_domain as rd

#: The header a client uses to assert which operator is acting.
ACTOR_HEADER = "X-Operator-Id"
#: Optional: correlates the decision with the caller's own request id.
CORRELATION_HEADER = "X-Correlation-Id"

#: An operator id is an opaque local identifier. Bounded and charset-limited so
#: it cannot smuggle markup, newlines or credential material into the audit log.
ACTOR_PATTERN = re.compile(r"[A-Za-z0-9_.:@-]{3,64}")

DENY_NO_ACTOR = "operator_identity_required"
DENY_INVALID_ACTOR = "operator_identity_invalid"
DENY_ACTOR_LOOKS_SECRET = "operator_identity_looks_secret"

#: Substrings that must never appear in an asserted identity. An operator id is
#: not a place to put a credential, and a caller doing so would leak it into the
#: audit trail.
_SECRET_MARKERS = ("bearer", "password", "secret", "token", "apikey", "api_key")


class DecisionAuthorizationError(PermissionError):
    """A decision the caller may not take. `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class DecisionPrincipal:
    """WHO is acting, and HOW MUCH that claim is worth. Immutable."""

    __slots__ = ("actor_id", "actor_type", "identity_assurance", "correlation_id")

    def __init__(self, *, actor_id: str, actor_type: str,
                 identity_assurance: str, correlation_id: str | None = None):
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "actor_type", actor_type)
        object.__setattr__(self, "identity_assurance", identity_assurance)
        object.__setattr__(self, "correlation_id", correlation_id)

    def __setattr__(self, *_args):                      # frozen
        raise AttributeError("DecisionPrincipal is immutable")

    @property
    def authenticated(self) -> bool:
        return self.identity_assurance == rd.IDENTITY_AUTHENTICATED

    def safe_view(self) -> dict:
        """The principal as it may be SHOWN: pseudonymized, never raw."""
        return {
            "actor": rd.pseudonymize_actor(self.actor_id),
            "actorType": self.actor_type,
            "identityAssurance": self.identity_assurance,
        }

    def __repr__(self) -> str:                          # never leaks the raw id
        return (f"DecisionPrincipal({rd.pseudonymize_actor(self.actor_id)}, "
                f"{self.identity_assurance})")


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def resolve_principal(*, asserted_actor, boundary_authenticated: bool,
                      correlation_id=None,
                      actor_type: str = rd.ActorType.OPERATOR) -> DecisionPrincipal:
    """Resolve the acting principal, or refuse.

    `boundary_authenticated` is supplied by the caller from the EXISTING auth
    boundary — this module does not re-derive it and never inspects a token.

    An absent or unusable identity is a refusal, not a fallback to "system":
    attributing a decision to a machine account that did not take it would
    corrupt the audit trail more quietly than refusing does.
    """
    actor = _clean(asserted_actor)
    if not actor:
        raise DecisionAuthorizationError(
            DENY_NO_ACTOR,
            f"assert the acting operator with the {ACTOR_HEADER} header")
    if not ACTOR_PATTERN.fullmatch(actor):
        raise DecisionAuthorizationError(
            DENY_INVALID_ACTOR,
            "operator id must be 3-64 chars of [A-Za-z0-9_.:@-]")
    lowered = actor.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise DecisionAuthorizationError(DENY_ACTOR_LOOKS_SECRET)

    correlation = _clean(correlation_id) or None
    if correlation is not None:
        if len(correlation) > rd.MAX_CORRELATION_ID_CHARS \
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", correlation):
            correlation = None              # unusable correlation is dropped,
                                            # never a reason to lose a decision
    return DecisionPrincipal(
        actor_id=actor, actor_type=actor_type,
        identity_assurance=(rd.IDENTITY_AUTHENTICATED if boundary_authenticated
                            else rd.IDENTITY_ASSERTED),
        correlation_id=correlation)


def boundary_is_authenticating() -> bool:
    """Whether the EXISTING API authentication boundary is actually enforcing.

    Read from `auth_policy` so there is exactly one source of truth. A
    misconfigured policy fails closed at the middleware, so it is not
    authenticating anything here either."""
    policy = auth_policy.load_policy()
    return bool(policy.enabled and not policy.misconfigured)


def assert_decision_allowed(principal: DecisionPrincipal, *,
                            decision_type: str) -> None:
    """The permission check itself: is this principal allowed this decision?

    Deliberately narrow. There are no roles, so the rule is structural rather
    than per-user: only an OPERATOR may take an operator decision, and only the
    three operator decision types exist. Automated types (AUTO_ACCEPT,
    SYSTEM_INVALIDATE) and internal ones (DEFER, WITHDRAW, SUPERSEDE) are not
    reachable through this gate at all — which is what keeps the write API from
    becoming a back door to autonomy.
    """
    if principal.actor_type != rd.ActorType.OPERATOR:
        raise DecisionAuthorizationError(
            "actor_type_not_permitted",
            f"{principal.actor_type} may not take operator decisions")
    if decision_type not in rd.OPERATOR_DECISION_TYPES:
        raise DecisionAuthorizationError(
            "decision_type_not_permitted",
            f"{decision_type} is not an operator decision")
