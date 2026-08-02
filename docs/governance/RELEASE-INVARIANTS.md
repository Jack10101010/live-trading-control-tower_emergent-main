# Release invariants

*M-RELEASE-CHECKPOINT-1. The properties this release is claiming, and the test
that enforces each one.*

**This file is a rendering of `backend/tests/test_release_checkpoint.py::INVARIANTS`.**
The list there is the source of truth; `test_invariant_is_documented` asserts
every id appears here, and `test_every_invariant_names_a_test_that_exists`
resolves every binding against the tests pytest can actually collect.

That second check is the point. The failure this programme kept finding was
never "a rule was broken" — it was "a rule was believed to be enforced by
something that had stopped enforcing it": a checker predicate that could never
be false, a guard satisfied by a comment, a warning computed by a function
nothing called. An invariant pointing at a renamed test reads exactly like one
that is enforced, so the pointer is checked too.

| id | invariant | enforced by |
|---|---|---|
| `ENV-1` | production cannot select the mock broker adapter | `test_environment_boundary.py::test_21b_the_mock_adapter_cannot_be_CONSTRUCTED_in_production` |
| `ENV-2` | production cannot select a fixture/mock_live/replay data provider | `test_environment_boundary.py::test_13_to_17_production_rejects_market_provider` |
| `FIX-1` | the ordinary frontend requests zero fixture endpoints | `frontend/src/lib/__tests__/ordinaryFixtureBoundary.test.ts` |
| `FIX-2` | importing the backend performs zero fixture loads | `test_world_isolation.py::test_1_server_import_performs_zero_fixture_file_reads` |
| `FIX-3` | every fixture-backed route is an explicit /api/dev/fixture-* path | `test_preview_namespace.py::test_1_no_fixture_backed_route_lacks_the_dev_namespace` |
| `FIX-4` | the mounted, derived and OpenAPI fixture inventories agree | `test_preview_namespace.py::test_1b_mounted_derived_and_openapi_inventories_agree` |
| `MOCK-1` | MockBroker never loads authored fixture data | `test_world_isolation.py::test_3b_the_mock_broker_never_invokes_the_fixture_loader` |
| `MOCK-2` | mock records never gain operational authority | `test_activation_certification.py::test_lattice_storage_and_endpoint_names_grant_nothing` |
| `AUTH-1` | node telemetry alone never grants account truth | `test_activation_guards.py::test_guard_1_node_telemetry_alone_cannot_create_broker_provenance` |
| `AUTH-2` | account truth never grants execution authority | `test_activation_certification.py::test_lattice_execution_authority_is_unreachable` |
| `AUTH-3` | account authority requires literal observation evidence | `test_activation_guards.py::test_guard_2_account_provenance_requires_observation_evidence` |
| `AUTH-4` | adapter configuration alone never creates live provenance | `test_activation_guards.py::test_guard_3_adapter_configuration_alone_cannot_create_live_provenance` |
| `AUTH-5` | the node and broker authority sets stay disjoint | `test_activation_guards.py::test_guard_14_node_and_broker_authority_functions_remain_disjoint` |
| `ADM-1` | admission fails closed on any non-boolean `admitted` | `test_activation_guards.py::test_guard_17_the_admission_gate_fails_closed_on_non_booleans` |
| `ADM-2` | missing availability can never become an implied yes | `test_activation_guards.py::test_guard_4_missing_availability_cannot_become_true` |
| `ADM-3` | refused accounts contribute to no metric or count | `frontend/src/views/__tests__/activationStates.test.tsx` |
| `ADM-4` | a pinned deployment refuses an unverifiable identity | `test_activation_e2e.py::test_21_a_pinned_deployment_refuses_an_account_with_no_identity` |
| `SCH-1` | an unknown schema version cannot downgrade to legacy | `test_activation_guards.py::test_guard_5_unknown_schema_cannot_be_admitted` |
| `SCH-2` | a refused payload cannot overwrite the last good observation | `test_activation_e2e.py::test_a_refused_payload_cannot_overwrite_a_good_one` |
| `FRESH-1` | stale data cannot appear current | `test_activation_certification.py::test_the_verdict_cannot_be_told_it_is_fresh` |
| `FRESH-2` | one freshness authority, and the node cannot widen its budget | `test_activation_certification.py::test_a_node_cannot_buy_itself_a_larger_freshness_budget` |
| `CONTRA-1` | contradictory genuine accounts are surfaced, never merged | `test_activation_guards.py::test_guard_7_contradictory_genuine_sources_are_surfaced` |
| `CONTRA-2` | a refused record never raises a contradiction | `test_activation_certification.py::test_a_refused_record_never_produces_a_contradiction` |
| `LEDGER-1` | ledger analytics admit MT5 execution origin only | `test_ledger_execution_origin.py::test_5_mock_can_never_appear_mt5_originated` |
| `NULL-1` | an unavailable value never becomes zero | `test_activation_guards.py::test_guard_13_unavailable_never_becomes_zero` |
| `CHK-1` | the activation checker is GET-only and read-only | `test_activation_guards.py::test_guard_9_the_activation_checker_is_read_only` |
| `CHK-2` | the checker verifies that the BACKEND is pinned, not just itself | `test_activation_checker_controls.py::test_an_unpinned_runtime_is_a_FAIL_not_a_warning` |
| `CHK-3` | every conditional check has a negative control | `test_activation_checker_controls.py::test_every_conditional_check_has_a_negative_control` |
| `CHK-4` | the checker leaks no token, path or full identifier | `test_activation_guards.py::test_guard_12b_the_checker_never_prints_a_token_or_an_absolute_path` |
| `UI-1` | frontend and backend admission semantics agree | `test_activation_certification.py::test_the_frontend_and_backend_admission_gates_agree` |
| `UI-2` | no admission decision is made outside the canonical seam | `test_activation_guards.py::test_guard_16_no_admission_decision_is_made_outside_the_canonical_seam` |
| `TEST-1` | no activation test writes the repository runtime DB | `test_activation_guards.py::test_guard_15_no_activation_test_writes_the_repository_runtime_db` |
| `REL-1` | the compatibility manifest matches the code it describes | `test_release_checkpoint.py::test_the_manifest_still_matches_the_code` |
| `REL-2` | the canonical map's derived facts match the code | `test_release_checkpoint.py::test_the_canonical_map_states_the_real_route_inventory` |
| `REL-3` | validation never overwrites frontend/dist | `test_release_checkpoint.py::test_the_validation_entry_point_never_writes_the_shipped_bundle` |

## Counting

**35 invariants, 35 bindings, 0 supported only by prose.**

`test_no_invariant_is_supported_only_by_prose` keeps that last number at zero.
An invariant that cannot be executed is a wish with a reference number, and the
honest thing is to say so in the list rather than to leave it looking the same
as the rest.

## What is deliberately NOT here

Four known boundaries, named in `CANONICAL-IMPLEMENTATION-MAP.md` §9, are not
invariants because they are not currently true: `/api/live-runtime` and the
positions/orders projections sit outside the identity pin, the contradiction
warning renders one page away from the balances it describes, and the freshness
phase budget remains node-influenced (clamped, not removed). Listing them as
invariants would be the exact defect this file exists to prevent.
