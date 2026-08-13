# Phase 1 — LR-1 Durable Intent Transaction (DESIGN ONLY)

**Workspace:** `live-trading-control-tower-live-prep` · branch `live-implementation-prep` · baseline `81e468c…640f9` (frozen)
**Status:** Design/architecture review only. No code written, no source modified, no diffs, no implementation.
**Invariants held immutable (Phase 0 PASS):** strategy behaviour, replay behaviour, deterministic outputs, byte-parity. The design below changes **none** of them — it adds durability around intents that are *already generated*, touching nothing the engine computes.

---

## The core insight (why this is small)

Two facts in the existing code make LR-1 fixable by *reusing* what's already there, not redesigning:

1. **`LEDGER_PENDING = "pending"` is defined (`state.py:17`) but never used anywhere.** The architecture already reserved a state for "durably recorded, not yet submitted." Phase 1 simply starts using it.
2. **`ledger_set(intent_id, status, detail)` (`state.py:69`) already accepts a free-form `detail` dict**, and the whole ledger + `last_boundary` live in one JSON blob written by one atomic `save()` (`state.py:43-47`, tmp+`os.replace`).

So the durable transaction record already exists (the ledger), the atomic commit already exists (`state.save`), the idempotent identity already exists (`intents.py:44` sha1), and the duplicate gate already exists (`safety.py:39-42`). **The defect is purely an *ordering/population* bug: the ledger is populated by the executor (after the boundary is already durable) instead of by the runner (atomically with the boundary).** Fix the ordering; invent nothing.

---

## Audit answers

### 1. Where is an intent first created?
- **Object:** `OrderIntent` (frozen dataclass) — `live/intents.py:28-41`.
- **Function:** `diff_frontier(prev_frame, cur_frame, frontier_bar)` — `live/intents.py:68`; ids via `_intent_id` (sha1) `intents.py:44-45`.
- **Caller:** `LiveRunner.run_once` — `live/runner.py:159` (`intents = diff_frontier(prev, trades_str, frontier_bar)`).
- **Lifetime today:** in-memory list only; returned in the run_once dict (`runner.py:167`). **Never persisted as intents.**

### 2. Where is runner state persisted?
- `RunnerState.save()` — `live/state.py:43-47` (atomic: write `.tmp`, `os.replace`). Writes the whole `self.data` (includes `last_boundary`, `prev_frame_*`, `ledger`, `mirror`, `daily`).
- Called at **three** points: `runner.py:163` (boundary + frame), `executor.py:81` (freeze branch), `executor.py:99` (ledger + mirror, end of `apply`).
- `last_boundary` is authored in exactly one place — `state.py:55` (inside `store_frame`), committed by `runner.py:163`.

### 3. Where does MT5 submission actually occur?
- `MT5Gateway.open_position / modify_position_sl / close_position` — `live/mt5_gateway.py:119 / 141 / 151`; the real call is `self.sdk.order_send(...)` at `mt5_gateway.py:136 / 146 / 165`.
- Reached only from `Executor._execute` — `executor.py:114 / 121 / 124` — and only when `config.mode == "live"` (`executor.py:104` guards dry-run; `main.py:84` refuses live in P1).

### 4. The unsafe window (real call sequence)

```
main.cycle()                                   main.py:47
  runner.run_once()                            main.py:56
    if last_boundary == boundary: no_new_bar   runner.py:148   ← replay gate keys ONLY on last_boundary
    trades = pipeline(...)                      runner.py:153
    intents = diff_frontier(prev, cur, bar)     runner.py:159   ← INTENTS born (memory only)
    state.store_frame(cur, boundary)            runner.py:162   ← sets last_boundary + prev_frame (memory)
    state.save()   ★ DURABLE COMMIT ★           runner.py:163   ← boundary+frame now on disk; intents are NOT
  ── returns intents (in memory) ──
  if status==ok and intents:                    main.py:57
    executor.apply(intents)                     main.py:58
      reconcile()                               executor.py:75
      ledger_set(SENT) / order_send / mirror    executor.py:112 / gateway:136 / 118   (all in-memory)
      state.save()   ★ ledger+mirror DURABLE ★   executor.py:99
```

**The window is `runner.py:163` → `executor.py:99`.** After `runner.py:163` the boundary is durably advanced but the intents exist only in RAM. A crash anywhere in that span means: restart → `run_once` hits `no_new_bar` at `runner.py:148` → intents never regenerated (and `prev_frame` already advanced at `runner.py:162`, so a forced re-diff yields nothing) → **the OPEN/CLOSE/MODIFY is permanently lost.** (Confirmed crash-matrix cases 2–4 in the reconciliation report.)

### 5. The smallest durable transaction

**What must become durable:** the generated intents, *atomically with* the `last_boundary`/`prev_frame` advance — i.e., before or in the same write that makes the boundary durable.

**Where durability occurs:** the **existing** single atomic `save()` at `runner.py:163`. Populate the ledger with the intents (as `PENDING`, payload in the existing `detail` dict) *before* that save. Because `last_boundary`, `prev_frame`, and `ledger` are all fields of the one `self.data` blob written by one `os.replace`, atomicity is free — no new commit mechanism.

**Ordering guarantee:** `diff_frontier` → reserve each intent `ledger_set(id, PENDING, {"intent": intent.to_dict()})` → `store_frame` → **one** `save()`. The boundary can never be durable unless its intents are durable in the same file. The gate at `runner.py:148` is thereby backed by a record that also contains the work to do.

**Replay after restart:** a **drain** step re-applies any `PENDING` (durably-reserved, unsubmitted) ledger intents through the *existing* `executor.apply` path, independent of the `no_new_bar` gate. Payload comes from the ledger `detail`, so no re-derivation from `prev_frame` is needed.

**Duplicate prevention:** the **existing** idempotent id (`intents.py:44`) + the **existing** duplicate rail (`safety.py:39-42`: any intent already `SENT/CONFIRMED/SIMULATED` is blocked). Draining an already-terminal intent is a no-op; draining a `PENDING` one submits it exactly once.

**Guarantee (corrected — no claim of broker-level exactly-once):** MT5 has no client-supplied idempotency key, so true broker-level exactly-once is not achievable. The guarantees actually delivered are: (a) **no generated intent is silently lost**; (b) **deterministic durable recovery of PENDING intents** after any crash; (c) **at-most-once automatic broker submission** — a SENT intent is never automatically resubmitted; (d) **reconciliation-and-freeze on an unknown SENT outcome** — ambiguity surfaces the exact intent id and reason rather than guessing.
- *Dry-run (current P1):* `PENDING → SIMULATED`. The drain plus the duplicate rail give exactly-once *in dry-run* trivially (no real broker).
- *Live (P2, future):* add one durability point — persist `SENT` *before* `order_send` (`executor.py:112` gains a `save()` before `gateway.open_position`). On restart a `SENT`-but-unconfirmed intent is **not** blindly resubmitted; it is resolved by the **existing** `reconcile()` (`executor.py:40`) against magic-tagged broker positions, freezing on ambiguity (`executor.py:60-65`). This closes crash cases 5–6 without new machinery.

### 6. Transaction state machine (reuses existing ledger states)

```
        (diff_frontier, in memory)
                 NEW
                  │  reserve in ledger, atomic with boundary  (runner.py:163)
                  ▼
              PENDING ★durable★            ← LEDGER_PENDING (state.py:17), first real use
        ┌─────────┼──────────────┐
   dry-run│         │live          │ rail reject
        ▼         ▼              ▼
    SIMULATED   SENT ★durable★   BLOCKED
   (terminal)    │ (persist BEFORE order_send)
                  │ order_send (mt5_gateway.py:136)
        ┌─────────┴─────────┐
        ▼                   ▼
     CONFIRMED            FAILED
     (terminal)          (terminal)
                  ▲
   reconcile()────┘  SENT-unconfirmed on restart → adopt/confirm, or FREEZE if unknown
```

Every state name already exists in `state.py:17-22`. The only currently-unused one, `PENDING`, becomes the durability anchor.

### 7. Restart recovery
- **Loaded:** `RunnerState._load()` (`state.py:36-41`) — `last_boundary`, `prev_frame_*`, `ledger` (now containing any `PENDING`/`SENT` intents with payload), `mirror`, `daily`.
- **Replayed:** a startup **drain** iterates ledger entries whose status is `PENDING` (and, live-only, resolves `SENT`-unconfirmed via `reconcile`) and pushes them through `executor.apply` using the persisted payload.
- **Cannot happen twice:** submission of any intent already `SENT/CONFIRMED/SIMULATED` — blocked by the duplicate rail (`safety.py:41`). Re-evaluation of the strategy for the committed boundary — blocked by `no_new_bar` (`runner.py:148`).
- **Guarantees no signal lost:** an intent is durable the instant its boundary is durable (same atomic write); the drain guarantees every `PENDING` intent is eventually applied; the boundary gate guarantees it is applied for the right (already-committed) bar without recomputation.

### 8. Interaction audit

| Component | Interaction | Change? |
|---|---|---|
| Runner state (`state.py`) | becomes the durable transaction record; intents reserved into `ledger` before the existing `save()`; add a read for `PENDING` entries | small, additive (new usage of existing `ledger`/`detail`) |
| Heartbeat (`ops_log`, `mt5_bridge`) | unaffected; LR-1 needs no liveness change (that's Phase 4) | none |
| Journal (`cycles.jsonl`, `ops_log.py`) | may optionally log drained-intent counts; format unchanged and tolerant reader (`ops_log.py:60-63`) | none required |
| Executor (`executor.py`) | gains `drain_pending()` reusing `apply`/`reconcile`/rails; live-only: persist `SENT` before `order_send` | small |
| MT5 bridge (`mt5_bridge.py`) | market-data only; untouched | none |
| Publisher (`publisher.py`) | already serialises intents/execution; drained results flow through naturally | none |
| Replay mode (`rehearsal.py`) | drain is a no-op when no `PENDING` exists; parity artifacts are trade frames, not the ledger → byte-parity preserved; `restart_no_extra_intents` still holds | none (must re-verify) |
| Dry-run mode | `PENDING → SIMULATED`; `order_ops` stays 0 → `dry_run_never_submits` preserved | none to behaviour |

### 9. Must any existing file change format?
**No breaking format change.** The ledger is a JSON dict inside `runner_state.json`; intents are stored via the **existing** free-form `detail` argument of `ledger_set` (`state.py:69`), and `PENDING` is an **existing** constant. Old state files still load (`_load` defaults `ledger` to `{}`, `state.py:39`). The only delta is *populating an already-supported field earlier and with more content* — additive and backward-compatible. `cycles.jsonl`, `heartbeat.json`, `publish_last.json`, and all Golden/parity artifacts are untouched. Justification for "no": the durable record and its schema already exist; Phase 1 uses them, it does not reshape them.

### 10. Migration plan (smallest behavioural delta — design steps, not code)
1. **Adopt `PENDING`:** on intent generation in `run_once`, reserve each intent in the ledger as `PENDING` with `{"intent": intent.to_dict()}` in `detail`, *before* `store_frame`/`save`, so the existing single atomic `save()` at `runner.py:163` commits boundary + frame + pending-intents together.
2. **Add `Executor.drain_pending()`:** re-apply ledger `PENDING` intents through the existing `apply` path (rails + reconcile + `_execute`), reconstructing `OrderIntent` from stored payload.
3. **Wire the drain once at startup** in `main()` (after `build()`, before the loop) — optionally also at cycle top (idempotent, cheap when empty). This is the only `main.py` change and it does not alter the happy-path cycle.
4. **(Live/P2 only) SENT-before-submit:** in `_execute`, persist `SENT` before `order_send`; on restart resolve `SENT`-unconfirmed via existing `reconcile`/freeze. Inert under dry-run.
5. **Add the LR-1 regression test** (the RED test specified in Phase 0 §9): crash between `runner.save` and `executor.apply` → restart → intents recovered, exactly once. Must fail on baseline, pass after steps 1–3.
6. **Re-run Phase 0 gates** (unit suite + Golden rehearsal on the Mac) to prove zero behavioural/parity/determinism delta.

---

## Deliverables

### 1. Current runtime sequence (defective)
```
bar close ─► run_once ─► diff_frontier (intent in RAM)
                         └► store_frame ─► save ★boundary durable, intents NOT★
          ─► executor.apply ─► ledger SENT/order_send/mirror ─► save ★intents durable★
   CRASH between the two saves ⇒ restart ⇒ no_new_bar ⇒ intent lost
```

### 2. Proposed runtime sequence (durable)
```
bar close ─► run_once ─► diff_frontier (intent in RAM)
                         ├► ledger.reserve(PENDING, payload)          ┐ one
                         ├► store_frame                                ├ atomic
                         └► save ★boundary + frame + PENDING intents★  ┘ save
          ─► executor.apply ─► (PENDING→SIMULATED | SENT→order_send→CONFIRMED) ─► save
   CRASH anywhere ⇒ restart ⇒ drain_pending() re-applies PENDING intents (exactly once)
```

### 3. Transaction state machine
`NEW →(atomic w/ boundary)→ PENDING →{ dry-run: SIMULATED | live: SENT →(order_send)→ CONFIRMED/FAILED } ; rail-reject → BLOCKED ; SENT-unconfirmed-on-restart → reconcile→confirm/FREEZE` (all states pre-existing; `PENDING` first used).

### 4. Crash recovery sequence
```
start ─► RunnerState._load (ledger incl. PENDING/SENT + payload)
      ─► Executor.drain_pending():
           for each ledger entry:
             PENDING            ─► apply via existing path ─► SIMULATED/SENT/CONFIRMED
             SENT-unconfirmed   ─► reconcile vs broker ─► confirm or FREEZE (live only)
             SIMULATED/CONFIRMED/BLOCKED ─► skip (duplicate rail / terminal)
      ─► enter normal loop (run_once → no_new_bar for the committed boundary)
guarantees: no intent lost (durable w/ boundary), none applied twice (idempotent id + rail),
            right bar (boundary gate), no strategy recompute on restart.
```

### 5. Files that would require modification (implementation phase, not now)
- `live/runner.py` — reserve intents as `PENDING` before the existing `save()` (`~:159-163`).
- `live/state.py` — small helper to reserve a pending intent + query undrained `PENDING` (reuses `ledger`/`detail`; no schema change).
- `live/executor.py` — add `drain_pending()`; live-only `SENT`-before-`order_send`.
- `live/main.py` — call `drain_pending()` once at startup.
- `backend/tests/test_live_slice.py` — add the LR-1 regression test.
- **No change:** `intents.py`, `mt5_gateway.py`, `mt5_bridge.py`, `publisher.py`, `ops_log.py`, `shadow_report.py`, `rehearsal.py`, and everything under Lux `strategy_core`.

### 6. Risk assessment
| Risk | Level | Basis / mitigation |
|---|---|---|
| Determinism / Golden parity | **Low** | No change to strategy, `diff_frontier`, or trade frames; ledger is not a parity artifact. Re-run rehearsal to confirm. |
| Duplicate submission | **Low** | Idempotent id + duplicate rail (existing); live: `SENT`-before-submit + reconcile. |
| File-format regression | **Low** | Additive use of existing `ledger`/`detail`/`PENDING`; old state loads unchanged. |
| Replay/dry-run behaviour | **Low** | Drain is a no-op when empty; `dry_run_never_submits`/`restart_no_extra_intents` preserved. |
| Complexity creep | **Low** | No db/queue/broker/threads/concurrency; reuses `apply`/`reconcile`/ledger/atomic-save. |
| Ordering subtlety | **Medium** | Drain must run before the boundary gate can cause a skip → wire at startup; covered by the new regression test. |

### 7. Recommendation
The fix is an *ordering and population* correction that reuses the ledger as the durable transaction record, the existing atomic `save()` as the commit, the already-defined-but-unused `PENDING` state as the anchor, and the existing idempotent-id + duplicate-rail + reconcile machinery for at-most-once submission and unknown-outcome (freeze) handling — not a claim of broker-level exactly-once. It introduces no new architecture, dependencies, storage, queues, or concurrency, changes no file format, and cannot alter strategy/replay/deterministic behaviour. It completely eliminates the LR-1 silent-loss window and closes the live crash cases via the existing freeze path.

---

## Verdict

**APPROVED FOR IMPLEMENTATION**

Scope note for the implementation phase: steps 1–3 + the regression test (5) fully eliminate LR-1 for the current dry-run P1 runtime; step 4 (SENT-before-submit + reconcile) is part of the same state machine but activates only under `LIVE_MODE=live` (P2) and must ship before live promotion. No code was written and no source was modified in this phase.
