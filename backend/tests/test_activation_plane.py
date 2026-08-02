"""ARCH-3 — the secure pre-live activation plane.

Covers: the three authentication principals (live-app middleware, cross-principal
denial, 401/503, CORS on failure), the canonical ConnectionPolicy (allow/deny
matrix, no-socket-before-allow, profiles), operator command authorization, the
node publisher's ingest credential, node-fact context assembly (authority,
staleness, account identity), and the truthful security/readiness surfaces.
"""

from __future__ import annotations

import io
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import auth_policy as ap                                            # noqa: E402
import broker as broker_layer                                       # noqa: E402
import command_authorization as ca                                  # noqa: E402
import connection_policy as cpol                                    # noqa: E402
import execution_context as xc                                      # noqa: E402
import execution_safety as es                                       # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)

OP_TOKEN = "operator-token-" + "o" * 32
IN_TOKEN = "ingest-token-" + "i" * 32
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def principals(monkeypatch):
    """Enable BOTH principals on the LIVE app (no copied middleware)."""
    monkeypatch.setattr(server, "_AUTH_POLICY", ap.load_policy(
        {ap.VAR_ENABLED: "1", ap.VAR_TOKEN: OP_TOKEN}))
    monkeypatch.setattr(server, "_INGEST_POLICY", ap.load_ingest_policy(
        {ap.VAR_INGEST_ENABLED: "1", ap.VAR_INGEST_TOKEN: IN_TOKEN}))
    monkeypatch.delenv(ap.VAR_INGEST_ALLOW_OPERATOR, raising=False)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


SNAPSHOT = {"schema_version": "ct.node-telemetry.v1", "instance_id": "t-1",
            "published_at": "2026-07-27T12:00:00Z"}


# ── principals: live-app matrix ───────────────────────────────────────────────

def test_operator_routes_accept_only_the_operator_credential(principals):
    assert client.get("/api/dev/fixture-world").status_code == 401
    assert client.get("/api/dev/fixture-world", headers=bearer(IN_TOKEN)).status_code == 401
    assert client.get("/api/dev/fixture-world", headers=bearer(OP_TOKEN)).status_code == 200


def test_ingest_route_accepts_only_the_ingest_credential(principals):
    # (payload is rejected as telemetry later — auth happens FIRST; a 400 proves
    # the request got PAST authentication.)
    anon = client.post("/api/live/ingest", json=SNAPSHOT)
    assert anon.status_code == 401
    operator = client.post("/api/live/ingest", json=SNAPSHOT, headers=bearer(OP_TOKEN))
    assert operator.status_code == 401                # cross-principal DENIES by default
    ingest = client.post("/api/live/ingest", json=SNAPSHOT, headers=bearer(IN_TOKEN))
    assert ingest.status_code != 401                  # authenticated (then validated)


def test_legacy_operator_on_ingest_only_when_explicitly_enabled(principals, monkeypatch):
    monkeypatch.setenv(ap.VAR_INGEST_ALLOW_OPERATOR, "1")
    degraded = client.post("/api/live/ingest", json=SNAPSHOT, headers=bearer(OP_TOKEN))
    assert degraded.status_code != 401                # deprecated compat path works
    body = client.get("/api/security/config", headers=bearer(OP_TOKEN)).json()
    assert body["ingestAuth"]["degraded"] is True     # ...and is reported degraded


def test_ingest_enabled_but_misconfigured_fails_closed_503(principals, monkeypatch):
    monkeypatch.setattr(server, "_INGEST_POLICY",
                        ap.load_ingest_policy({ap.VAR_INGEST_ENABLED: "1"}))
    r = client.post("/api/live/ingest", json=SNAPSHOT, headers=bearer(IN_TOKEN))
    assert r.status_code == 503
    assert r.json()["code"] == ap.CODE_MISCONFIGURED


def test_live_app_operator_misconfiguration_fails_closed_503(monkeypatch):
    monkeypatch.setattr(server, "_AUTH_POLICY", ap.load_policy({ap.VAR_ENABLED: "1"}))
    r = client.get("/api/live/status")
    assert r.status_code == 503 and r.json()["code"] == ap.CODE_MISCONFIGURED
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/health").status_code == 200          # probe stays open


def test_live_401_still_carries_the_cors_origin_header(principals):
    r = client.get("/api/live/status", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_ingest_disabled_leaves_the_route_open_even_with_operator_auth_on(monkeypatch):
    monkeypatch.setattr(server, "_AUTH_POLICY", ap.load_policy(
        {ap.VAR_ENABLED: "1", ap.VAR_TOKEN: OP_TOKEN}))
    monkeypatch.setattr(server, "_INGEST_POLICY", ap.load_ingest_policy({}))
    r = client.post("/api/live/ingest", json=SNAPSHOT)
    assert r.status_code != 401                       # documented: enable BOTH principals


def test_no_credential_appears_in_any_auth_response(principals):
    for r in (client.get("/api/dev/fixture-world"),
              client.post("/api/live/ingest", json=SNAPSHOT)):
        assert OP_TOKEN not in r.text and IN_TOKEN not in r.text


# ── ConnectionPolicy ──────────────────────────────────────────────────────────

FULL_ENV = {
    "CONTROL_TOWER_TRANSPORT_ENABLED": "1",
    "NODE_TRANSPORT": "https",
    "NODE_ENDPOINT": "http://127.0.0.1:9",
    "NODE_API_TOKEN": "node-token-" + "n" * 32,
    "ALLOW_INSECURE_LOCALHOST": "1",
}


def test_local_loopback_allows_only_loopback():
    d = cpol.evaluate("http://127.0.0.1:9/health", env=dict(FULL_ENV))
    assert d.allowed and d.reason == cpol.ALLOW_LOCAL_LOOPBACK
    assert cpol.evaluate("https://localhost:9/x", env=dict(FULL_ENV)).allowed


def test_remote_plain_http_denies():
    d = cpol.evaluate("http://10.0.0.5:8000/health", env=dict(FULL_ENV))
    assert not d.allowed and d.reason == cpol.DENY_REMOTE_PLAIN_HTTP


def test_remote_https_denies_under_the_local_profile():
    d = cpol.evaluate("https://node.example:8443/h", env=dict(FULL_ENV))
    assert not d.allowed and d.reason == cpol.DENY_HOST_NOT_LOOPBACK


@pytest.mark.parametrize("profile", [cpol.PROFILE_REMOTE_PRE_LIVE, cpol.PROFILE_REMOTE_LIVE])
def test_remote_profiles_deny_and_name_missing_prerequisites(profile):
    env = dict(FULL_ENV, **{cpol.VAR_PROFILE: profile})
    d = cpol.evaluate("https://node.example:8443/h", env=env)
    assert not d.allowed and d.reason == cpol.DENY_PROFILE_NOT_APPROVED
    assert "private_network_attested" in d.missing_prerequisites
    assert "remote_activation_attested" in d.missing_prerequisites


def test_unknown_profile_and_disabled_and_invalid_are_distinct():
    assert cpol.evaluate("http://127.0.0.1:9/", env=dict(FULL_ENV, **{
        cpol.VAR_PROFILE: "yolo"})).reason == cpol.DENY_PROFILE_UNKNOWN
    disabled = dict(FULL_ENV); disabled.pop("CONTROL_TOWER_TRANSPORT_ENABLED")
    assert cpol.evaluate("http://127.0.0.1:9/", env=disabled).reason \
        == cpol.DENY_TRANSPORT_DISABLED
    invalid = dict(FULL_ENV, NODE_ENDPOINT="http://u:p@127.0.0.1:9")   # creds-in-URL = error
    assert cpol.evaluate("http://127.0.0.1:9/", env=invalid).reason \
        == cpol.DENY_CONFIG_INVALID


def test_missing_auth_bad_scheme_and_bad_endpoint_deny():
    no_token = dict(FULL_ENV); no_token.pop("NODE_API_TOKEN")
    assert cpol.evaluate("http://127.0.0.1:9/", env=no_token).reason \
        == cpol.DENY_AUTH_NOT_CONFIGURED
    assert cpol.evaluate("ftp://127.0.0.1:9/", env=dict(FULL_ENV)).reason \
        == cpol.DENY_SCHEME_NOT_ALLOWED
    assert cpol.evaluate("not-a-url", env=dict(FULL_ENV)).reason \
        == cpol.DENY_ENDPOINT_INVALID


def test_policy_error_denies_and_decision_is_immutable(monkeypatch):
    monkeypatch.setattr(cpol, "urlsplit", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    d = cpol.evaluate("http://127.0.0.1:9/", env=dict(FULL_ENV))
    assert not d.allowed and d.reason == cpol.DENY_POLICY_ERROR
    with pytest.raises(Exception):
        d.allowed = True


def test_safe_view_carries_no_host_or_secret():
    d = cpol.evaluate("https://node.example:8443/h", env=dict(FULL_ENV))
    view = str(d.safe_view())
    assert "node.example" not in view
    assert FULL_ENV["NODE_API_TOKEN"] not in view


def test_no_socket_opens_before_the_policy_allows(monkeypatch):
    """A denied policy means urlopen is NEVER reached."""
    import rest_transport as rt
    opened = []
    transport = rt.RestTransport("https://node.example:8443", "t" * 40)
    monkeypatch.setattr(transport._opener, "open",
                        lambda *a, **k: opened.append(1))
    result = transport.health()
    assert result.reason == rt.REASON_CONNECTION_DENIED
    assert result.detail == cpol.DENY_HOST_NOT_LOOPBACK
    assert opened == [], "a socket path ran despite a policy deny"


def test_every_outbound_request_consults_the_policy():
    # Structural: the single I/O method calls the policy before the opener, and
    # no other urlopen/open call exists in the transport.
    code = code_only("rest_transport.py")
    assert code.index("connection_policy.evaluate") < code.index("self._opener.open")
    assert code.count("self._opener.open") == 1
    assert "urlopen" not in code


def test_canonical_policy_satisfies_the_ui9_protocol():
    import security_config as sc
    policy = cpol.CanonicalConnectionPolicy("http://127.0.0.1:9/", dict(FULL_ENV))
    assert isinstance(policy, sc.ConnectionPolicy)
    assert policy.may_connect() is True


# ── operator command authorization ────────────────────────────────────────────

def _grant(**over):
    kw = dict(authorization_id=ca.new_authorization_id(), operator_ref="op-7",
              issued_at="2026-07-27T11:00:00Z", expires_at="2026-07-27T13:00:00Z",
              allowed_risk_classes=frozenset({"execution_affecting"}),
              allowed_scopes=frozenset({"dpl_1"}), confirmed=True, provider="test")
    kw.update(over)
    return ca.AuthorizationGrant(**kw)


def test_grant_is_immutable_scope_exact_and_bounded():
    g = _grant()
    with pytest.raises(Exception):
        g.confirmed = False
    assert g.authorizes(risk_class="execution_affecting", scope="dpl_1", now=NOW)
    assert not g.authorizes(risk_class="execution_affecting", scope="dpl_2", now=NOW)
    assert not g.authorizes(risk_class="emergency", scope="dpl_1", now=NOW)
    late = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
    assert not g.authorizes(risk_class="execution_affecting", scope="dpl_1", now=late)


def test_revocation_and_malformed_dates_fail_closed():
    g = ca.revoke(_grant())
    assert g.revoked and not g.is_active(NOW)
    bad = _grant(expires_at="not-a-date")
    assert not bad.is_active(NOW)
    unconfirmed = _grant(confirmed=False)
    assert not unconfirmed.is_active(NOW)


def test_grant_safe_view_masks_the_operator():
    view = _grant(operator_ref="login:secret-operator password=P@SS").safe_view()
    assert "P@SS" not in str(view)
    assert view["wildcardScope"] is False


def test_mock_provider_refuses_non_mock_adapters():
    provider = ca.MockAuthorizationProvider(
        active_adapter_kind_fn=lambda: "mt5", operator_ref_fn=lambda: "op")
    assert provider.current_grant(NOW) is None
    mock = ca.MockAuthorizationProvider(
        active_adapter_kind_fn=lambda: "mock", operator_ref_fn=lambda: "op")
    g = mock.current_grant(NOW)
    assert g is not None and g.provider == "mock"
    assert g.safe_view()["wildcardScope"] is True     # explicit + visible


def test_tower_authorization_cannot_substitute_for_node_facts():
    """A grant plus nothing else still denies: node health, account identity and
    mode gates run independently of authorization."""
    ctx = xc.ExecutionContext(authorization=_grant(allowed_scopes=frozenset({ca.SCOPE_ANY})))
    d = es.evaluate(es.CommandRequest(command_type="CloseTrade"),
                    ctx.to_safety_context("CloseTrade", now=NOW))
    assert d.allowed is False                          # observe mode/node unknown deny


# ── node facts, staleness, account identity ───────────────────────────────────

def _live_ctx(**over):
    kw = dict(
        execution_mode=es.MODE_ACTIVE,
        operator=es.OperatorAuthorization("op", True),
        authorization=_grant(allowed_scopes=frozenset({ca.SCOPE_ANY})),
        account_identity_state=es.ACCOUNT_MATCH,
        node=xc.NodeFacts(health=es.NODE_HEALTHY, stale=False,
                          provenance=xc.PROV_NODE_TELEMETRY),
    )
    kw.update(over)
    return xc.ExecutionContext(**kw)


def test_stale_node_telemetry_denies_execution():
    ctx = _live_ctx(node=xc.NodeFacts(health=es.NODE_HEALTHY, stale=True,
                                      provenance=xc.PROV_NODE_TELEMETRY))
    d = es.evaluate(es.CommandRequest(command_type="CloseTrade"),
                    ctx.to_safety_context("CloseTrade", now=NOW))
    assert d.allowed is False and d.reason == "node_stale"


def test_account_mismatch_denies_everything_non_read_only():
    ctx = _live_ctx(account_identity_state=es.ACCOUNT_MISMATCH)
    for cmd in ("CloseTrade", "GlobalKill", "LockDeployment"):
        d = es.evaluate(es.CommandRequest(command_type=cmd),
                        ctx.to_safety_context(cmd, now=NOW))
        assert d.allowed is False and d.reason == es.DENY_ACCOUNT_MISMATCH
    read = es.evaluate(es.CommandRequest(command_type="request_health"),
                       ctx.to_safety_context("request_health", now=NOW))
    assert read.allowed is True


def test_account_unknown_denies_execution_affecting_only():
    ctx = _live_ctx(account_identity_state=es.ACCOUNT_UNKNOWN)
    d = es.evaluate(es.CommandRequest(command_type="CloseTrade"),
                    ctx.to_safety_context("CloseTrade", now=NOW))
    assert d.reason == es.DENY_ACCOUNT_UNKNOWN
    halt = es.evaluate(es.CommandRequest(command_type="GlobalKill"),
                       ctx.to_safety_context("GlobalKill", now=NOW))
    assert halt.allowed is True                       # a halt stays possible


def test_node_arming_facts_are_observed_never_synthesized():
    facts = server._node_facts()
    assert facts.provenance in (xc.PROV_ABSENT, xc.PROV_NODE_TELEMETRY)
    if facts.provenance == xc.PROV_ABSENT:
        assert facts.arming_armed is None and facts.health == es.NODE_UNKNOWN


def test_account_state_derivation(monkeypatch):
    node = xc.NodeFacts(account_fingerprint="acctfp_abc", stale=False,
                        provenance=xc.PROV_NODE_TELEMETRY)
    monkeypatch.setenv(server.VAR_EXPECTED_ACCOUNT, "acctfp_abc")
    assert server._account_identity_state(node) == es.ACCOUNT_MATCH
    monkeypatch.setenv(server.VAR_EXPECTED_ACCOUNT, "acctfp_zzz")
    assert server._account_identity_state(node) == es.ACCOUNT_MISMATCH
    monkeypatch.delenv(server.VAR_EXPECTED_ACCOUNT)
    assert server._account_identity_state(node) == es.ACCOUNT_UNKNOWN
    stale = xc.NodeFacts(account_fingerprint="acctfp_abc", stale=True,
                         provenance=xc.PROV_NODE_TELEMETRY)
    monkeypatch.setenv(server.VAR_EXPECTED_ACCOUNT, "acctfp_abc")
    assert server._account_identity_state(stale) == es.ACCOUNT_UNKNOWN   # stale can't authorize


def test_no_permissive_context_for_non_mock_adapters(monkeypatch):
    monkeypatch.setattr(broker_layer, "active_kind", lambda: "mt5")
    ctx = server._execution_context()
    assert ctx.authorization is None
    assert ctx.execution_mode == es.MODE_OBSERVE
    # LIVE-3 (deliberate evolution): the non-mock context is now assembled from
    # DURABLE governance (mode owner + durable grants) — still deny-by-default:
    # observe mode and no grant unless explicitly issued.
    assert dict(ctx.provenance)["tower"] == "durable-governance"


# ── node publisher (ingest credential; HTTP ≠ network failure) ────────────────

class _Cfg:
    state_dir = None
    ct_base_url = "http://127.0.0.1:1/api"
    ct_ingest_token = IN_TOKEN


def _publisher(tmp_path):
    from live.publisher import CTPublisher
    cfg = _Cfg()
    cfg.state_dir = str(tmp_path)
    return CTPublisher(cfg)


def test_publisher_sends_the_ingest_bearer_token(tmp_path, monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.headers.get("Authorization")
        raise urllib.error.URLError("stop here")
    import live.publisher as pub
    monkeypatch.setattr(pub.urllib.request, "urlopen", fake_urlopen)
    result = _publisher(tmp_path).publish({"x": 1})
    assert seen["auth"] == f"Bearer {IN_TOKEN}"
    assert result["delivered"] is False and "unauthorized" not in result


def test_publisher_distinguishes_401_from_network_failure(tmp_path, monkeypatch):
    import live.publisher as pub
    def raise_401(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b""))
    monkeypatch.setattr(pub.urllib.request, "urlopen", raise_401)
    r = _publisher(tmp_path).publish({"x": 1})
    assert r == {"delivered": False, "status": 401, "unauthorized": True,
                 "error": "http 401", "fallback": r["fallback"]}
    assert IN_TOKEN not in str(r)

    def raise_net(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(pub.urllib.request, "urlopen", raise_net)
    r2 = _publisher(tmp_path).publish({"x": 1})
    assert "unauthorized" not in r2 and r2["delivered"] is False


def test_publisher_writes_the_fallback_before_any_network_attempt(tmp_path, monkeypatch):
    """Trading state is never hostage to the tower: the durable fallback exists
    even when the very first network call explodes."""
    import live.publisher as pub
    def boom(req, timeout=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(pub.urllib.request, "urlopen", boom)
    p = _publisher(tmp_path)
    p.publish({"cycle": 42})
    assert p.fallback.exists() and "42" in p.fallback.read_text()


# ── truthful surfaces ─────────────────────────────────────────────────────────

def test_security_surface_reports_all_dimensions_without_stale_claims():
    body = client.get("/api/security/config").json()
    assert "active" not in body and "activeReason" not in body
    text = body.__repr__()
    assert "exist yet" not in text                    # the stale claim is gone
    assert body["connectivity"]["remoteApproved"] is False
    assert body["connectivity"]["profile"] == "local_loopback"
    assert {"transport", "connectionPolicy", "outboundNodeAuth"} <= set(body["connectivity"])


def test_transport_enabled_but_invalid_is_distinct_from_disabled(monkeypatch):
    import transport as t
    disabled = t.selection_status({})
    invalid = t.selection_status({"CONTROL_TOWER_TRANSPORT_ENABLED": "1"})
    assert disabled["enabled"] is False and disabled["misconfigured"] is False
    assert invalid["enabled"] is True and invalid["misconfigured"] is True
    assert disabled["reason"] != invalid["reason"]


def test_public_health_body_is_minimal_when_enforcing(principals):
    body = client.get("/api/health").json()
    assert set(body) == {"status", "scope", "serverTime"}
    for leak in ("brokerKind", "nodeTelemetry", "fixture", "liveNodeConnected"):
        assert leak not in body
    rich = client.get("/api/health", headers=bearer(OP_TOKEN)).json()
    assert "brokerKind" in rich


def test_no_store_is_applied_uniformly_to_api_routes():
    for path in ("/api/health", "/api/dev/fixture-world", "/api/live/status",
                 "/api/execution/state", "/api/security/config"):
        r = client.get(path)
        assert r.headers.get("cache-control") == "no-store", path


def test_readiness_gates_include_the_activation_plane_gates():
    gates = client.get("/api/execution/state").json()["readiness"]["gates"]
    for gate in ("operatorAuthenticated", "commandAuthorizationActive",
                 "accountIdentityMatch", "approvedConnectionProfile",
                 "liveAdapterActive", "reconciliationClean"):
        assert gate in gates
    assert client.get("/api/execution/state").json()["readiness"]["tradingReady"] is False


def test_new_route_and_surface_classification_is_exact():
    # every route classified exactly once across the three classes
    paths = {r.path for r in server.app.routes if isinstance(getattr(r, "path", None), str)}
    for p in paths:
        classes = [c for c in (ap.CLASS_PUBLIC, ap.CLASS_PROTECTED, ap.CLASS_INGEST)
                   if ap.classify_route(p) == c]
        assert len(classes) == 1, p
