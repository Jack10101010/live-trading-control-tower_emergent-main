/**
 * UI-3 — live-operations derivation.
 *
 * Three invariants are asserted repeatedly, because they are the ones a display
 * layer is most likely to violate quietly:
 *
 *   CRITICAL WINS       a healthy field can never lower the reported severity
 *   STALE ERASES        past the threshold nothing describes the present
 *   ABSENCE ≠ HEALTH    unknown and unhealthy stay distinct; neither is healthy
 *
 * Field names below were re-derived from a real `/api/live/status` response, not
 * from the shape suggested in the specification.
 */
import { describe, it, expect } from 'vitest';
import type {
  ConnectionInstance,
  ConnectionState,
  LiveStatus,
  LiveStatusEntry,
  TelemetrySnapshot,
} from '@/lib/api';
import {
  deriveLiveOperations,
  mapReasonCodes,
  orderInstanceOps,
  worstSeverity,
} from '@/lib/liveOperations';

/* ── builders mirroring the real contract ───────────────────────────────────*/

function snapshot(over: Partial<TelemetrySnapshot> = {}): TelemetrySnapshot {
  return {
    schema_version: 'ct.node-telemetry.v1',
    instance_id: 'node-1',
    published_at: '2026-07-25T12:00:00Z',
    runtime: {
      mode: 'dry_run',
      submission_disabled: false,
      kill_switch_active: false,
      open_eligibility: { eligible: null, reasons: ['authorization_not_evaluated_by_node_rails'] },
    },
    reconciliation: {
      available: true, clean: true, frozen: false, recovery_required: false,
      unresolved_sent_count: 0, snapshot_status: 'ok',
    },
    account: {
      identity: { available: true, fingerprint: 'acctfp_abc' },
      health: { available: true, healthy: true, trade_allowed: true, trade_expert: true, reasons: [] },
    },
    arming: {
      status: 'armed', armed: true, expires_at: '2099-01-01T00:00:00Z',
      attempts_remaining: 1, fingerprint_matches: true, reason: null,
    },
    execution: { cycle_frozen: false, pending_intents: [], blocks: [], unresolved_sent: [] },
    engine: { symbol: 'EURUSD', timeframe: 'M15' },
    market: { available: true, feed_healthy: true },
    ...over,
  };
}

function entry(over: Partial<LiveStatusEntry> = {}): LiveStatusEntry {
  return {
    instance_id: 'node-1',
    schema_version: 'ct.node-telemetry.v1',
    legacy_source: false,
    published_at: '2026-07-25T12:00:00Z',
    observed_at: '2026-07-25T12:00:05Z',
    received_at: '2026-07-25T12:00:01Z',
    age_seconds: 5,
    stale: false,
    stale_after_seconds: 120,
    snapshot: snapshot(),
    ...over,
  };
}

function connInstance(over: Partial<ConnectionInstance> = {}): ConnectionInstance {
  return {
    instanceId: 'node-1', telemetry: 'fresh', node: 'connected',
    nodeEvidence: 'fresh telemetry received from the node',
    bridge: 'healthy', bridgeEvidence: 'node observed a healthy price feed',
    problem: null, schemaVersion: 'ct.node-telemetry.v1', legacySource: false,
    publishedAt: '2026-07-25T12:00:00Z', receivedAt: '2026-07-25T12:00:01Z',
    ageSeconds: 5, staleAfterSeconds: 120, mode: 'dry_run', engineVersion: '5bb6372c',
    ...over,
  };
}

function connection(over: Partial<ConnectionState> = {}): ConnectionState {
  const instances = over.instances ?? [connInstance()];
  return {
    observedAt: '2026-07-25T12:00:05Z', staleAfterSeconds: 120, firstRun: false,
    telemetry: 'fresh', node: 'connected', bridge: 'healthy',
    instanceCount: instances.length, instances, emptyState: null,
    ...over,
  };
}

function status(entries: LiveStatusEntry[]): LiveStatus {
  return {
    schemaVersion: 'ct.node-telemetry.v1',
    observedAt: '2026-07-25T12:00:05Z',
    instances: entries.map((e) => e.instance_id),
    statuses: Object.fromEntries(entries.map((e) => [e.instance_id, e])),
    emptyState: null,
  };
}

/** Derive with one snapshot override, everything else nominal. */
function withSnapshot(over: Partial<TelemetrySnapshot>) {
  const e = entry({ snapshot: snapshot(over) });
  return deriveLiveOperations('available', connection(), status([e]));
}

const chip = (ops: ReturnType<typeof deriveLiveOperations>, key: string) =>
  ops.instances[0].chips.find((c) => c.key === key)!;

/* ── severity ordering ──────────────────────────────────────────────────────*/

describe('severity precedence', () => {
  it('takes the worst, never an average', () => {
    expect(worstSeverity(['healthy', 'critical', 'warning'])).toBe('critical');
    expect(worstSeverity(['healthy', 'warning', 'unknown'])).toBe('warning');
    expect(worstSeverity(['healthy', 'unknown'])).toBe('unknown');
    expect(worstSeverity(['healthy', 'healthy'])).toBe('healthy');
    expect(worstSeverity([])).toBe('unknown');
  });

  it('never lets one healthy field override a critical field', () => {
    const ops = withSnapshot({
      runtime: { mode: 'dry_run', submission_disabled: false, kill_switch_active: true,
                 open_eligibility: { eligible: false, reasons: ['kill_switch_active'] } },
    });
    expect(chip(ops, 'reconciliation').severity).toBe('healthy');
    expect(chip(ops, 'account').severity).toBe('healthy');
    expect(ops.severity).toBe('critical');
  });
});

/* ── baseline states ────────────────────────────────────────────────────────*/

describe('no snapshot / first run', () => {
  it('is unknown and non-optimistic', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ firstRun: true, telemetry: 'never_received', node: 'unknown',
                   bridge: 'unknown', instances: [], instanceCount: 0,
                   emptyState: 'No execution node has ever published telemetry.' }),
      undefined
    );
    expect(ops.severity).toBe('unknown');
    expect(ops.firstRun).toBe(true);
    expect(ops.instances).toEqual([]);
    for (const word of ['healthy', 'armed', 'live', 'ready', 'connected', 'safe']) {
      expect(ops.headline.toLowerCase()).not.toContain(word);
    }
  });
});

describe('backend unavailable', () => {
  it('reports unknown and blames nothing on the node', () => {
    const ops = deriveLiveOperations('unavailable', undefined, undefined);
    expect(ops.severity).toBe('unknown');
    expect(ops.node).toBe('unknown');
    expect(ops.bridge).toBe('unknown');
    expect(ops.headline).toMatch(/node keeps running without it/i);
  });

  it('ignores any cached connection payload while unreachable', () => {
    const ops = deriveLiveOperations('unavailable', connection(), status([entry()]));
    expect(ops.severity).toBe('unknown');
    expect(ops.instances).toEqual([]);
  });
});

describe('fresh healthy snapshot', () => {
  it('is healthy only with affirmative evidence on every dimension', () => {
    const ops = deriveLiveOperations('available', connection(), status([entry()]));
    expect(ops.severity).toBe('healthy');
    expect(chip(ops, 'kill').value).toBe('clear');
    expect(chip(ops, 'reconciliation').value).toBe('clean');
    expect(chip(ops, 'account').value).toBe('healthy');
    expect(chip(ops, 'arming').value).toBe('armed');
    expect(chip(ops, 'submission').value).toBe('enabled');
  });

  it('reports dry-run as healthy evidence, not as a fault', () => {
    const ops = deriveLiveOperations('available', connection(), status([entry()]));
    expect(chip(ops, 'mode').value).toBe('dry_run');
    expect(chip(ops, 'mode').severity).toBe('unknown');   // mode is never a verdict
    expect(ops.severity).toBe('healthy');
  });

  it('treats rehearsal mode the same way', () => {
    const ops = withSnapshot({
      runtime: { mode: 'rehearsal', submission_disabled: false, kill_switch_active: false,
                 open_eligibility: { eligible: null, reasons: [] } },
    });
    expect(chip(ops, 'mode').value).toBe('rehearsal');
    expect(ops.severity).toBe('healthy');
  });

  it('never makes live mode healthy by itself', () => {
    const ops = withSnapshot({
      runtime: { mode: 'live', submission_disabled: false, kill_switch_active: false,
                 open_eligibility: { eligible: null, reasons: [] } },
      account: { health: { available: false } },        // no affirmative evidence
      arming: { status: 'unarmed', armed: false },
    });
    expect(chip(ops, 'mode').value).toBe('live');
    expect(ops.severity).not.toBe('healthy');
  });
});

/* ── stale ──────────────────────────────────────────────────────────────────*/

describe('stale telemetry', () => {
  const staleOps = () =>
    deriveLiveOperations(
      'available',
      connection({ telemetry: 'stale', node: 'unknown', bridge: 'unknown',
                   instances: [connInstance({ telemetry: 'stale', node: 'unknown',
                                              bridge: 'unknown', ageSeconds: 9000 })] }),
      status([entry({ stale: true, age_seconds: 9000 })])
    );

  it('never stays healthy on previously healthy data', () => {
    const ops = staleOps();
    expect(ops.severity).not.toBe('healthy');
    expect(ops.instances[0].stale).toBe(true);
  });

  it('does not present armed or bridge-healthy as current', () => {
    const ops = staleOps();
    const arming = chip(ops, 'arming');
    expect(arming.value).toContain('stale');
    expect(arming.severity).toBe('unknown');
    expect(arming.historical).toBe(true);
    expect(ops.bridge).toBe('unknown');
  });

  it('keeps the last observed value visible as labelled history', () => {
    const ops = staleOps();
    expect(chip(ops, 'reconciliation').detail).toMatch(/last observed/i);
    expect(chip(ops, 'reconciliation').historical).toBe(true);
  });

  it('still exposes the snapshot timestamp and age', () => {
    const ops = staleOps();
    expect(ops.instances[0].freshness.publishedAt).toBe('2026-07-25T12:00:00Z');
    expect(ops.instances[0].freshness.ageSeconds).toBe(9000);
  });
});

/* ── critical conditions ────────────────────────────────────────────────────*/

describe('critical conditions', () => {
  it('kill switch active', () => {
    const ops = withSnapshot({
      runtime: { mode: 'dry_run', submission_disabled: false, kill_switch_active: true,
                 open_eligibility: { eligible: false, reasons: ['kill_switch_active'] } },
    });
    expect(chip(ops, 'kill').value).toBe('ENGAGED');
    expect(ops.severity).toBe('critical');
  });

  it('reconciliation frozen', () => {
    const ops = withSnapshot({
      reconciliation: { available: true, frozen: true, clean: false, recovery_required: true,
                        unresolved_sent_count: 0, snapshot_status: 'unreadable' },
    });
    expect(chip(ops, 'reconciliation').value).toBe('FROZEN');
    expect(ops.severity).toBe('critical');
  });

  it('recovery required without a freeze', () => {
    const ops = withSnapshot({
      reconciliation: { available: true, frozen: false, clean: false, recovery_required: true,
                        unresolved_sent_count: 0, snapshot_status: 'ok' },
    });
    expect(chip(ops, 'reconciliation').value).toBe('recovery required');
    expect(ops.severity).toBe('critical');
  });

  it('account explicitly unhealthy', () => {
    const ops = withSnapshot({
      account: { health: { available: true, healthy: false, trade_allowed: true, trade_expert: true } },
    });
    expect(chip(ops, 'account').value).toBe('blocked');
    expect(ops.severity).toBe('critical');
  });

  it('trade not allowed', () => {
    const ops = withSnapshot({
      account: { health: { available: true, healthy: null, trade_allowed: false, trade_expert: true } },
    });
    expect(chip(ops, 'account').value).toBe('trade not allowed');
    expect(ops.severity).toBe('critical');
  });

  it('expert trading not allowed', () => {
    const ops = withSnapshot({
      account: { health: { available: true, healthy: null, trade_allowed: true, trade_expert: false } },
    });
    expect(chip(ops, 'account').value).toBe('expert trading off');
    expect(ops.severity).toBe('critical');
  });

  it('unresolved SENT records', () => {
    const ops = withSnapshot({
      reconciliation: { available: true, clean: false, frozen: false, recovery_required: false,
                        unresolved_sent_count: 2, snapshot_status: 'ok' },
    });
    expect(chip(ops, 'unresolved').value).toBe('2');
    expect(ops.severity).toBe('critical');
  });

  it('fingerprint mismatch outranks an otherwise armed session', () => {
    const ops = withSnapshot({
      arming: { status: 'armed', armed: true, fingerprint_matches: false, attempts_remaining: 1 },
    });
    expect(chip(ops, 'arming').value).toBe('account mismatch');
    expect(ops.severity).toBe('critical');
  });

  it('bridge unavailable while telemetry is fresh', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ bridge: 'unavailable',
                   instances: [connInstance({ bridge: 'unavailable' })] }),
      status([entry()])
    );
    expect(ops.severity).toBe('critical');
  });

  it('node disconnected while telemetry is fresh', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ node: 'disconnected', instances: [connInstance({ node: 'disconnected' })] }),
      status([entry()])
    );
    expect(ops.severity).toBe('critical');
  });

  it('does not escalate bridge or node to critical without fresh telemetry', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ telemetry: 'stale', bridge: 'unavailable', node: 'disconnected',
                   instances: [connInstance({ telemetry: 'stale', bridge: 'unavailable',
                                              node: 'disconnected' })] }),
      status([entry({ stale: true })])
    );
    expect(ops.severity).toBe('warning');    // stale, not a confirmed fault
  });
});

/* ── warning conditions ─────────────────────────────────────────────────────*/

describe('warning conditions', () => {
  it('submission disabled', () => {
    const ops = withSnapshot({
      runtime: { mode: 'dry_run', submission_disabled: true, kill_switch_active: false,
                 open_eligibility: { eligible: false, reasons: ['submission_disabled'] } },
    });
    expect(chip(ops, 'submission').value).toBe('disabled');
    expect(ops.severity).toBe('warning');
  });

  it.each([
    ['unarmed', 'unarmed'],
    ['expired', 'expired'],
    ['exhausted', 'exhausted'],
    ['disarmed', 'disarmed'],
  ])('arming %s is a warning', (armStatus, expected) => {
    const ops = withSnapshot({ arming: { status: armStatus, armed: false } });
    expect(chip(ops, 'arming').value).toBe(expected);
    expect(ops.severity).toBe('warning');
  });

  it('OPEN explicitly blocked', () => {
    const ops = withSnapshot({
      runtime: { mode: 'dry_run', submission_disabled: false, kill_switch_active: false,
                 open_eligibility: { eligible: false, reasons: ['not_armed'] } },
    });
    expect(chip(ops, 'open').value).toBe('blocked');
    expect(ops.severity).toBe('warning');
  });

  it('bridge degraded', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ bridge: 'degraded', instances: [connInstance({ bridge: 'degraded' })] }),
      status([entry()])
    );
    expect(ops.severity).toBe('warning');
  });

  it('a recent execution block', () => {
    const ops = withSnapshot({
      execution: { cycle_frozen: false, pending_intents: [], unresolved_sent: [],
                   blocks: [{ rail: 'spread_ceiling', intent_id: 'i-1' }] },
    });
    expect(ops.instances[0].counts.recentBlocks).toBe(1);
    expect(ops.severity).toBe('warning');
  });
});

/* ── unknown conditions ─────────────────────────────────────────────────────*/

describe('unknown conditions', () => {
  it('OPEN not evaluated is unknown, never allowed', () => {
    const ops = deriveLiveOperations('available', connection(), status([entry()]));
    expect(chip(ops, 'open').value).toBe('not evaluated');
    expect(chip(ops, 'open').severity).toBe('unknown');
  });

  it('account health unavailable is unknown, not unhealthy', () => {
    const ops = withSnapshot({ account: { health: { available: false } } });
    expect(chip(ops, 'account').value).toBe('unknown');
    expect(chip(ops, 'account').severity).toBe('unknown');
    expect(ops.severity).toBe('unknown');
  });

  it('a legacy snapshot lacking evidence is unknown, not healthy', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ bridge: 'unknown', instances: [connInstance({ bridge: 'unknown', legacySource: true })] }),
      status([entry({
        legacy_source: true,
        snapshot: snapshot({
          runtime: { mode: null, submission_disabled: null, kill_switch_active: null,
                     open_eligibility: { eligible: null, reasons: [] } },
          account: { health: { available: false } },
          arming: { status: 'unarmed', armed: false },
          reconciliation: { available: false },
        }),
      })])
    );
    expect(ops.instances[0].identity.legacySource).toBe(true);
    expect(chip(ops, 'kill').severity).toBe('unknown');
    expect(ops.severity).not.toBe('healthy');
  });

  it('an unusable snapshot reports unavailable, not stale interpretation', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ telemetry: 'unavailable', node: 'unknown', bridge: 'unknown',
                   instances: [connInstance({ telemetry: 'unavailable', node: 'unknown',
                                              bridge: 'unknown', problem: 'unsupported_schema' })] }),
      status([entry()])
    );
    expect(ops.instances[0].unavailable).toBe(true);
    expect(ops.instances[0].chips.map((c) => c.key)).toEqual(['snapshot']);
    expect(ops.instances[0].reasons[0].code).toBe('unsupported_schema');
  });
});

/* ── reason codes ───────────────────────────────────────────────────────────*/

describe('reason codes', () => {
  it('preserves the code and maps readable text', () => {
    const [first] = mapReasonCodes(['kill_switch_active']);
    expect(first.code).toBe('kill_switch_active');
    expect(first.text).toMatch(/kill switch/i);
    expect(first.unmapped).toBe(false);
  });

  it('renders an unknown code verbatim instead of dropping it', () => {
    const [only] = mapReasonCodes(['some_future_rail_code']);
    expect(only.code).toBe('some_future_rail_code');
    expect(only.text).toBe('some_future_rail_code');
    expect(only.unmapped).toBe(true);
    expect(only.severity).toBe('warning');   // never optimistic about the unknown
  });

  it('deduplicates repeated codes', () => {
    const mapped = mapReasonCodes(['not_armed', 'not_armed', 'not_armed']);
    expect(mapped).toHaveLength(1);
  });

  it('drops only empty values, never real codes', () => {
    const mapped = mapReasonCodes(['not_armed', null, undefined, '', '   ']);
    expect(mapped.map((r) => r.code)).toEqual(['not_armed']);
  });

  it('orders by severity, worst first', () => {
    const mapped = mapReasonCodes([
      'authorization_not_evaluated_by_node_rails',
      'submission_disabled',
      'kill_switch_active',
    ]);
    expect(mapped.map((r) => r.code)).toEqual([
      'kill_switch_active',
      'submission_disabled',
      'authorization_not_evaluated_by_node_rails',
    ]);
  });

  it('never invents a more optimistic explanation than the code supports', () => {
    const mapped = mapReasonCodes(['runtime_identity_mismatch']);
    expect(mapped[0].severity).toBe('critical');
    expect(mapped[0].text).not.toMatch(/\b(ok|fine|healthy|nominal)\b/i);
  });

  it('collects codes from eligibility, arming, health and execution blocks', () => {
    const ops = withSnapshot({
      runtime: { mode: 'live', submission_disabled: true, kill_switch_active: false,
                 open_eligibility: { eligible: false, reasons: ['submission_disabled'] } },
      arming: { status: 'expired', armed: false, reason: 'arm_expired' },
      account: { health: { available: false, reasons: ['account_health_unavailable'] } },
      execution: { blocks: [{ rail: 'spread_ceiling' }], pending_intents: [], unresolved_sent: [] },
    });
    const codes = ops.instances[0].reasons.map((r) => r.code);
    expect(codes).toContain('submission_disabled');
    expect(codes).toContain('arm_expired');
    expect(codes).toContain('account_health_unavailable');
    expect(codes).toContain('spread_ceiling');
  });
});

/* ── multiple instances ─────────────────────────────────────────────────────*/

describe('multiple instances', () => {
  const twoInstances = () => {
    const critical = entry({
      instance_id: 'node-bad',
      snapshot: snapshot({
        instance_id: 'node-bad',
        runtime: { mode: 'live', submission_disabled: false, kill_switch_active: true,
                   open_eligibility: { eligible: false, reasons: ['kill_switch_active'] } },
      }),
    });
    return deriveLiveOperations(
      'available',
      connection({ instances: [connInstance(), connInstance({ instanceId: 'node-bad' })],
                   instanceCount: 2 }),
      status([entry(), critical])
    );
  };

  it('keeps every instance and does not assume one', () => {
    expect(twoInstances().instances).toHaveLength(2);
  });

  it('rolls the fleet up to the worst instance', () => {
    const ops = twoInstances();
    const byId = Object.fromEntries(ops.instances.map((i) => [i.instanceId, i.severity]));
    expect(byId['node-1']).toBe('healthy');
    expect(byId['node-bad']).toBe('critical');
    expect(ops.severity).toBe('critical');
  });

  it('orders worst-first so a critical instance is never below a healthy one', () => {
    const ordered = orderInstanceOps(twoInstances().instances);
    expect(ordered[0].instanceId).toBe('node-bad');
  });
});

/* ── fixture isolation ──────────────────────────────────────────────────────*/

describe('fixture isolation', () => {
  it('cannot produce any state without live-node telemetry', () => {
    // No connection payload at all: the model has nothing else to fall back on,
    // and must not manufacture operational state from anywhere.
    const ops = deriveLiveOperations('available', undefined, status([entry()]));
    expect(ops.instances).toEqual([]);
    expect(ops.severity).toBe('unknown');
  });

  it('reports no instances when connection lists none, even if statuses exist', () => {
    const ops = deriveLiveOperations(
      'available',
      connection({ instances: [], instanceCount: 0 }),
      status([entry()])
    );
    expect(ops.instances).toEqual([]);
    expect(ops.severity).toBe('unknown');
  });
});

describe('severity contribution', () => {
  it('OPEN "not evaluated" alone does not prevent healthy', () => {
    // Regression: feeding this permanent, correct state into the aggregate made
    // `healthy` unreachable, so the whole severity scale collapsed to unknown.
    const ops = deriveLiveOperations('available', connection(), status([entry()]));
    expect(chip(ops, 'open').value).toBe('not evaluated');
    expect(chip(ops, 'open').contributes).toBe(false);
    expect(ops.severity).toBe('healthy');
  });

  it('OPEN "blocked" does contribute', () => {
    const ops = withSnapshot({
      runtime: { mode: 'dry_run', submission_disabled: false, kill_switch_active: false,
                 open_eligibility: { eligible: false, reasons: ['not_armed'] } },
    });
    expect(chip(ops, 'open').contributes).not.toBe(false);
    expect(ops.severity).toBe('warning');
  });

  it('runtime mode never contributes in either direction', () => {
    for (const mode of ['dry_run', 'live', 'rehearsal']) {
      const ops = withSnapshot({
        runtime: { mode, submission_disabled: false, kill_switch_active: false,
                   open_eligibility: { eligible: null, reasons: [] } },
      });
      expect(chip(ops, 'mode').contributes).toBe(false);
      expect(ops.severity).toBe('healthy');
    }
  });
});
