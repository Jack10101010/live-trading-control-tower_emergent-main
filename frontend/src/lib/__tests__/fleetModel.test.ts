/**
 * M-CT-FLEET-DASHBOARD-2 — the dashboard routes node verdicts; it never decides.
 *
 * The L_2106 incident: a parallel interpretation disagreed with the rails that
 * actually execute, and the parallel one was believed. These pin that every
 * verdict traces to the node, that the five clocks stay independent, and that
 * absence never becomes green.
 */
import { describe, it, expect } from 'vitest';
import {
  livenessSeverity, runtimeSeverity, cycleSeverity, readinessView,
  deliverySeverity, pendingSlots, reconciliationView, attentionItems,
  fleetHealth, humanAge, clean, safeAccountLabel, SEV_TONE, isBrokerExecuted, splitLedger, isSimulated, deliveryView, lifecycleOf, sortNewestFirst,
} from '@/lib/fleetModel';

const node = (over: any = {}) => ({
  instanceId: 'live-eurusd-golden-001',
  heartbeat: { available: true, status: 'live', ageSeconds: 7 },
  current: { cycleStatus: 'recomputing', strategyBlocksPresent: false },
  lastComplete: { available: true, isCurrent: false, ageSeconds: 660 },
  node: {
    snapshotReceivedAt: new Date().toISOString(),
    executionReadiness: {
      status: 'ready', eligible: true, reasons: [], evaluated_at: '2026-08-13T13:12:53Z',
      checks: { authorization: { ok: true, detail: '' }, news: { ok: true, detail: '6 events' } },
      candidate_checks_not_evaluated: { duplicate_intent: 'needs a real intent_id',
                                        stale_open: 'needs the candidate fill_time' },
    },
    reconciliation: { available: true, clean: null, frozen: false },
    nodeDelivery: { ct_delivery: 'healthy', pending_runtime: false,
                    pending_complete: false, pending_heartbeat: false },
    killSwitchActive: false,
  },
  ...over,
});

describe('five clocks stay independent', () => {
  it('a live heartbeat with an old cycle: node OK, cycle informational', () => {
    const v = node();
    expect(livenessSeverity(v)).toBe('ok');          // 7s beat
    expect(cycleSeverity(v)).toBe('info');           // 11m complete, recomputing
    expect(SEV_TONE[cycleSeverity(v)]).not.toBe('red');
  });

  it('a fresh heartbeat NEVER makes an old cycle look current', () => {
    const v = node({ lastComplete: { available: true, isCurrent: false, ageSeconds: 3600 } });
    expect(livenessSeverity(v)).toBe('ok');
    expect(cycleSeverity(v)).toBe('warn');           // genuinely delayed
  });

  it('an old runtime snapshot with a live heartbeat is amber, not red', () => {
    const v = node({ node: { ...node().node, snapshotReceivedAt: '2020-01-01T00:00:00Z' } });
    expect(runtimeSeverity(v)).toBe('warn');
    expect(livenessSeverity(v)).toBe('ok');          // the node is demonstrably alive
  });

  it('a dead heartbeat is RED regardless of how fresh the cycle is', () => {
    const v = node({ heartbeat: { available: true, status: 'offline', ageSeconds: 900 },
                     lastComplete: { available: true, isCurrent: true, ageSeconds: 2 } });
    expect(livenessSeverity(v)).toBe('bad');
    expect(SEV_TONE.bad).toBe('red');
  });

  it('no heartbeat observed is UNKNOWN, never healthy', () => {
    expect(livenessSeverity(node({ heartbeat: { available: false } }))).toBe('unknown');
    expect(SEV_TONE.unknown).not.toBe('green');
  });
});

describe('execution readiness is the node\'s verdict, verbatim', () => {
  it('renders READY only from an authoritative ready status', () => {
    const r = readinessView(node());
    expect(r.severity).toBe('ok');
    expect(r.label).toBe('READY');
    expect(r.available).toBe(true);
  });

  it('renders the node\'s blocked reasons exactly', () => {
    const v = node({ node: { ...node().node, executionReadiness: {
      status: 'blocked', eligible: false, reasons: ['arm_server_mismatch'], checks: {} } } });
    const r = readinessView(v);
    expect(r.severity).toBe('bad');
    expect(r.label).toBe('OPEN BLOCKED');
    expect(r.reasons).toEqual(['arm_server_mismatch']);   // not paraphrased
  });

  it('keeps candidate-specific checks visibly unevaluated', () => {
    const r = readinessView(node());
    expect(r.candidateNotEvaluated.map((c) => c.name).sort())
      .toEqual(['duplicate_intent', 'stale_open']);
  });

  it('absent readiness is UNKNOWN and never green', () => {
    const r = readinessView(node({ node: { ...node().node, executionReadiness: null } }));
    expect(r.severity).toBe('unknown');
    expect(r.label).toBe('READINESS UNKNOWN');
    expect(SEV_TONE[r.severity]).not.toBe('green');
  });

  it('carries evaluated_at so the verdict can be aged, not implied current', () => {
    expect(readinessView(node()).evaluatedAt).toBe('2026-08-13T13:12:53Z');
  });
});

describe('reconciliation tri-state', () => {
  it('clean:null is UNKNOWN, never PASS', () => {
    const r = reconciliationView(node());
    expect(r.label).toBe('UNKNOWN');
    expect(r.severity).toBe('unknown');
  });
  it('frozen is RED', () => {
    const v = node({ node: { ...node().node, reconciliation: { available: true, frozen: true } } });
    expect(reconciliationView(v).severity).toBe('bad');
  });
  it('clean:true is CLEAN', () => {
    const v = node({ node: { ...node().node, reconciliation: { available: true, clean: true, frozen: false } } });
    expect(reconciliationView(v).label).toBe('CLEAN');
  });
  it('unavailable is UNKNOWN', () => {
    const v = node({ node: { ...node().node, reconciliation: { available: false } } });
    expect(reconciliationView(v).severity).toBe('unknown');
  });
});

describe('CT delivery is transport, never execution', () => {
  it('degraded delivery does not make execution unhealthy', () => {
    const v = node({ node: { ...node().node, nodeDelivery: {
      ct_delivery: 'degraded', pending_heartbeat: true, pending_runtime: false, pending_complete: false } } });
    expect(deliverySeverity(v)).toBe('warn');
    expect(readinessView(v).severity).toBe('ok');       // untouched
    expect(livenessSeverity(v)).toBe('ok');
    expect(pendingSlots(v)).toEqual(['heartbeat']);
  });
  it('its attention item says trading is unaffected', () => {
    const v = node({ node: { ...node().node, nodeDelivery: { ct_delivery: 'degraded' } } });
    const item = attentionItems(v).find((a) => a.title === 'CT delivery degraded');
    expect(item?.detail).toContain('node trading unaffected');
  });
});

describe('attention rail holds only actionable items', () => {
  it('a fully healthy node produces none', () => {
    const v = node({ current: { cycleStatus: 'ok' },
                     lastComplete: { available: true, isCurrent: true, ageSeconds: 5 },
                     node: { ...node().node, reconciliation: { available: true, clean: true, frozen: false } } });
    expect(attentionItems(v)).toEqual([]);
  });
  it('a blocked node surfaces the node\'s own reason', () => {
    const v = node({ node: { ...node().node, executionReadiness: {
      status: 'blocked', eligible: false, reasons: ['arm_server_mismatch'], checks: {} } } });
    expect(attentionItems(v)[0]).toMatchObject({ sev: 'bad', title: 'Execution blocked',
                                                 detail: 'arm_server_mismatch' });
  });
  it('an active kill switch is red', () => {
    const v = node({ node: { ...node().node, killSwitchActive: true } });
    expect(attentionItems(v).some((a) => a.title.includes('Kill switch'))).toBe(true);
  });
});

describe('fleet aggregation names the offender', () => {
  it('one healthy node', () => {
    const h = fleetHealth([node({ node: { ...node().node,
      reconciliation: { available: true, clean: true, frozen: false } } })]);
    expect(h.sev).toBe('ok');
    expect(h.detail).toBe('1 node healthy');
  });
  it('multi-node: the failing node is named, not collapsed', () => {
    const bad = node({ instanceId: 'node-b',
      heartbeat: { available: true, status: 'offline', ageSeconds: 900 } });
    const h = fleetHealth([node(), bad]);
    expect(h.sev).toBe('bad');
    expect(h.detail).toBe('node-b');
  });
  it('no nodes is unknown, not healthy', () => {
    expect(fleetHealth([]).sev).toBe('unknown');
  });
});

describe('safety and formatting', () => {
  it('never renders an account login — fingerprint only', () => {
    const label = safeAccountLabel({ identity: { fingerprint: 'acctfp_4f1ea8b8aa7c8c24', login: 12345678 } });
    expect(label).not.toContain('12345678');
    expect(label).toContain('acctfp_');
  });
  it('"nan" is absence', () => {
    expect(clean('nan')).toBeNull();
    expect(clean('Bear/Chop')).toBe('Bear/Chop');
  });
  it('ages format compactly', () => {
    expect(humanAge(7)).toBe('7s');
    expect(humanAge(660)).toBe('11m');
    expect(humanAge(null)).toBeNull();
  });
});

describe('a simulated trade can never look broker-executed', () => {
  // The real local record: same durable store, mock underneath.
  const mockTrade = {
    tradeId: 'trd_ea63af4e1f6ae63d', provenance: 'durable-store', executionOrigin: 'mock',
    detail: { trade: { provenance: 'mock-fixture',
      lineage: { adapter: 'mock', broker: 'mock-fixture', accountFingerprint: 'acc_mock' } } },
  };
  const brokerTrade = {
    tradeId: 'trd_real', provenance: 'durable-store', executionOrigin: 'broker',
    detail: { trade: { provenance: 'live_mt5',
      lineage: { adapter: 'mt5', broker: 'FTMO-Demo', accountFingerprint: 'acctfp_4f1e' } } },
  };

  it('the mock-adapter record is NOT broker-executed', () => {
    expect(isBrokerExecuted(mockTrade)).toBe(false);
  });
  it('a genuine broker record is', () => {
    expect(isBrokerExecuted(brokerTrade)).toBe(true);
  });
  it('splitLedger keeps them apart', () => {
    const { broker, simulated } = splitLedger([mockTrade, brokerTrade]);
    expect(broker).toHaveLength(1);
    expect(simulated).toHaveLength(1);
    expect(broker[0].tradeId).toBe('trd_real');
  });
  it('fails closed on an unclassifiable record', () => {
    expect(isBrokerExecuted({ tradeId: 'x' })).toBe(false);
    expect(isBrokerExecuted(null)).toBe(false);
  });
});

describe('delivery authority age is part of its epistemic status', () => {
  const withDelivery = (ct: string, ageS: number, pending = {}) => node({
    node: { ...node().node,
      nodeDelivery: { ct_delivery: ct, pending_runtime: false, pending_complete: false,
                      pending_heartbeat: false, ...pending },
      deliveryAt: new Date(Date.now() - ageS * 1000).toISOString() } });

  it('a 19-minute-old degraded report does NOT assert current degradation', () => {
    // The exact live case: heartbeat 4s, delivery report 19m old saying pending.
    const v = withDelivery('degraded', 19 * 60, { pending_heartbeat: true });
    const dv = deliveryView(v);
    expect(dv.aged).toBe(true);
    expect(dv.label).toBe('STATUS AGED');
    expect(dv.severity).toBe('unknown');
    expect(dv.severity).not.toBe('warn');       // not a current fault
  });

  it('the aged report preserves its historical facts', () => {
    const dv = deliveryView(withDelivery('degraded', 19 * 60, { pending_heartbeat: true }));
    expect(dv.reported).toContain('degraded');
    expect(dv.reported).toContain('heartbeat');
    expect(dv.ageSeconds).toBeGreaterThan(1000);
  });

  it('an aged authority yields an "aged" attention item, not a transport failure', () => {
    const items = attentionItems(withDelivery('degraded', 19 * 60, { pending_heartbeat: true }));
    const aged = items.find((a) => a.title === 'CT delivery status aged');
    expect(aged).toBeTruthy();
    expect(aged!.detail).toContain('current transport state unknown');
    expect(items.find((a) => a.title === 'CT delivery degraded')).toBeUndefined();
  });

  it('a FRESH genuinely-degraded report still asserts DEGRADED', () => {
    const dv = deliveryView(withDelivery('degraded', 20, { pending_complete: true }));
    expect(dv.aged).toBe(false);
    expect(dv.severity).toBe('warn');
    expect(dv.label).toBe('DEGRADED');
    const item = attentionItems(withDelivery('degraded', 20, { pending_complete: true }))
      .find((a) => a.title === 'CT delivery degraded');
    expect(item?.detail).toContain('node trading unaffected');
  });

  it('a fresh healthy report is OK', () => {
    expect(deliveryView(withDelivery('healthy', 15)).severity).toBe('ok');
  });

  it('an aged report is never upgraded to healthy by a live heartbeat', () => {
    const v = withDelivery('degraded', 19 * 60, { pending_heartbeat: true });
    expect(livenessSeverity(v)).toBe('ok');            // beat is fresh
    expect(deliveryView(v).severity).toBe('unknown');  // delivery still unknown
    expect(deliveryView(v).severity).not.toBe('ok');
  });
});

describe('ledger provenance has three buckets, not two', () => {
  const mock = { executionOrigin: 'mock', detail: { trade: { lineage: { adapter: 'mock' } } } };
  const real = { executionOrigin: 'broker', detail: { trade: { lineage: { adapter: 'mt5', broker: 'FTMO-Demo' } } } };
  const murky = { tradeId: 'x' };                       // no provenance at all

  it('unknown provenance is UNVERIFIED, never broker', () => {
    const { broker, simulated, unverified } = splitLedger([mock, real, murky]);
    expect(broker).toHaveLength(1);
    expect(simulated).toHaveLength(1);
    expect(unverified).toHaveLength(1);
    expect(isBrokerExecuted(murky)).toBe(false);
    expect(isSimulated(murky)).toBe(false);
  });
});

describe('terminal outcomes are not presented as resting orders', () => {
  it('PENDING + never_triggered is CLOSED, not PENDING', () => {
    const l = lifecycleOf({ action: 'PENDING', cancel_reason: 'never_triggered', outcome: 'UNFILLED' });
    expect(l.label).toBe('CLOSED · NO FILL');
    expect(l.terminal).toBe(true);
    expect(l.label).not.toBe('PENDING');
  });
  it('PENDING + invalidated_before_edge_entry is terminal', () => {
    expect(lifecycleOf({ action: 'PENDING', cancel_reason: 'invalidated_before_edge_entry' }).terminal).toBe(true);
  });
  it('REFUSED with a state block reads REFUSED', () => {
    const l = lifecycleOf({ action: 'REFUSED', eligible: false, refusal_reason: 'state_target_block' });
    expect(l.label).toBe('REFUSED');
  });
  it('a filled record reads FILLED', () => {
    expect(lifecycleOf({ action: 'PENDING', fill_time: '2026-08-13 09:00:00+00:00' }).label).toBe('FILLED');
  });
  it('a genuinely open pending record still reads PENDING', () => {
    const l = lifecycleOf({ action: 'PENDING', cancel_reason: 'nan', outcome: 'nan' });
    expect(l.label).toBe('PENDING');
    expect(l.terminal).toBe(false);
  });
  it('sorts newest first', () => {
    const out = sortNewestFirst([{ utc: '2026-08-01T00:00:00Z' }, { utc: '2026-08-12T00:00:00Z' }]);
    expect(out[0].utc).toBe('2026-08-12T00:00:00Z');
  });
});
