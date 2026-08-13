/**
 * M-NODE-READ-1 — genuine node truth is visible; it grants no broker truth.
 *
 * THE DEFECT
 *   `NodeOperationalView` carries provenance `node-telemetry`, and every
 *   ordinary surface filtered nodes through `authoritativeOnly` — the BROKER
 *   gate. `node-telemetry` is correctly not in that set, so every node view was
 *   dropped: with a VPS node publishing validated telemetry each cycle, Fleet
 *   Overview said "No authoritative operational source" and the ScopeNavigator
 *   showed "—".
 *
 * WHY THE FIX IS TWO GATES AND NOT A WIDER ONE
 *   Adding `node-telemetry` to the broker set would have been one line and
 *   wrong: that set also guards balances, positions, orders and analytics
 *   admission, so a heartbeat would have implied an account.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import {
  PROV_NODE_TELEMETRY,
  PROV_FIXTURE_NODE,
  PROV_NODE_ABSENT,
  isAuthoritativeNodeObservation,
  authoritativeNodesOnly,
  classifyNodeObservation,
  nodeCardProvenance,
  NODE_ABSENT_DETAIL,
} from '@/lib/nodeProvenance';
import {
  isAuthoritative,
  PROV_LIVE_MT5,
  PROV_NODE_MT5,
  PROV_MOCK_FIXTURE,
} from '@/lib/operationalProvenance';

const state = vi.hoisted(() => ({ fleet: {} as Record<string, unknown> }));

vi.mock('@/hooks/useRepository', () => ({
  useOperationalFleet: () => state.fleet,
  useAccountsProtection: () => state.fleet,
  useConfiguredInstruments: () => ({ symbols: ['EURUSD'], configured: true }),
}));

import { FleetOverview } from '@/views/FleetOverview';

function node(over: Record<string, unknown> = {}) {
  return {
    nodeId: 'vps-node-1', provenance: PROV_NODE_TELEMETRY,
    lifecycleState: 'current', degradedReasons: [] as string[],
    deploymentProfile: 'vps-dry-run', nodeMode: 'dry_run', cycleStatus: 'ok',
    lastBoundary: '2026-08-02T11:45:00Z', lastBarTime: '2026-08-02T11:44:00Z',
    engineVersion: 'lux@1.4.2', symbol: 'EURUSD', timeframe: 'M15',
    killSwitchActive: false, submissionDisabled: true,
    openPositionCount: 0, openOrderCount: null,
    livenessAgeSeconds: 12, freshnessBasis: 'received_at',
    mt5Observation: null, legacySource: false, warnings: [] as string[],
    freshness: { available: true, stale: false },
    ...over,
  };
}

function setFleet(over: Record<string, unknown> = {}) {
  state.fleet = {
    nodes: [], accounts: [], deployments: [],
    status: 'unavailable', detail: 'no authoritative account source',
    // The SHIPPED copy, not a paraphrase — so a test asserting on the wording
    // is asserting on what an operator actually reads.
    nodeStatus: 'absent', nodeDetail: NODE_ABSENT_DETAIL,
    ...over,
  };
}

const renderFleet = () => render(<MemoryRouter><FleetOverview /></MemoryRouter>);
beforeEach(() => setFleet());

// ── 1–4  admission ───────────────────────────────────────────────────────────

describe('the node gate', () => {
  it('PROOF 1 — admits canonical node telemetry as node truth', () => {
    expect(isAuthoritativeNodeObservation(node())).toBe(true);
    expect(authoritativeNodesOnly([node()])).toHaveLength(1);
  });

  it('PROOF 2 — node telemetry is NOT broker truth, in both directions', () => {
    expect(isAuthoritative({ provenance: PROV_NODE_TELEMETRY })).toBe(false);
    for (const brokerOrigin of [PROV_LIVE_MT5, PROV_NODE_MT5, PROV_MOCK_FIXTURE]) {
      expect(isAuthoritativeNodeObservation({ provenance: brokerOrigin }), brokerOrigin)
        .toBe(false);
    }
  });

  it('PROOF 3 — fixture nodes are rejected', () => {
    expect(isAuthoritativeNodeObservation({ provenance: PROV_FIXTURE_NODE })).toBe(false);
    expect(authoritativeNodesOnly([{ provenance: PROV_FIXTURE_NODE }, node()]))
      .toHaveLength(1);
  });

  it('PROOF 25 — unknown provenance fails closed', () => {
    for (const p of ['node_telemetry', 'NODE-TELEMETRY', 'remote-node', 'mock',
                     PROV_NODE_ABSENT, '']) {
      expect(isAuthoritativeNodeObservation({ provenance: p }), p).toBe(false);
    }
    expect(isAuthoritativeNodeObservation(null)).toBe(false);
    expect(isAuthoritativeNodeObservation({})).toBe(false);
  });

  it('PROOF 4 — an absent source yields absent, never a zero-node metric', () => {
    expect(classifyNodeObservation([], { sourceAnswered: false })).toBe('absent');
    expect(classifyNodeObservation([], { sourceAnswered: true })).toBe('absent');
  });
});

// ── 5–9  the state model stays distinct ──────────────────────────────────────

describe('lifecycle states never collapse into one empty state', () => {
  it('classifies current, stale and degraded separately', () => {
    const opts = { sourceAnswered: true };
    expect(classifyNodeObservation([node()], opts)).toBe('current');
    expect(classifyNodeObservation([node({ lifecycleState: 'stale' })], opts)).toBe('stale');
    expect(classifyNodeObservation([node({ lifecycleState: 'degraded' })], opts)).toBe('degraded');
  });

  it('rolls up WORST-first so one bad node is never hidden behind a good one', () => {
    const opts = { sourceAnswered: true };
    expect(classifyNodeObservation(
      [node(), node({ nodeId: 'b', lifecycleState: 'degraded' })], opts)).toBe('degraded');
    expect(classifyNodeObservation(
      [node(), node({ nodeId: 'b', lifecycleState: 'stale' })], opts)).toBe('stale');
  });

  it('PROOF 8 — there is no `stopped` state, and none is synthesised', () => {
    // v1 carries no stopped signal: a node that halts simply stops publishing,
    // which is indistinguishable from one that cannot reach this tower.
    const status = classifyNodeObservation([node({ lifecycleState: 'stale' })],
      { sourceAnswered: true });
    expect(status).toBe('stale');
    expect(status).not.toBe('stopped');
  });
});

// ── 5–9  visual provenance ───────────────────────────────────────────────────

describe('visual provenance', () => {
  it('PROOFS 5–7 — current, stale and degraded nodes are ALL live cards', () => {
    // A stale node is real data that is old; a degraded node is real data about
    // real trouble. Neither is fixture data, so neither takes the red border
    // that means "this is not operational data".
    for (const lifecycle of ['current', 'stale', 'degraded']) {
      expect(nodeCardProvenance(node({ lifecycleState: lifecycle })), lifecycle)
        .toBe('live');
    }
  });

  it('a rejected record is a neutral placeholder, never a live card', () => {
    expect(nodeCardProvenance({ provenance: PROV_FIXTURE_NODE })).toBe('placeholder');
    expect(nodeCardProvenance({ provenance: PROV_NODE_ABSENT })).toBe('placeholder');
  });
});

// ── rendering ────────────────────────────────────────────────────────────────

describe('Fleet Overview shows a reporting node', () => {
  it('PROOF 1 — a genuine node is no longer invisible', () => {
    setFleet({ nodes: [node()], nodeStatus: 'current' });
    renderFleet();
    expect(screen.getByText('vps-node-1')).toBeTruthy();
    expect(screen.getByText('vps-dry-run')).toBeTruthy();
    expect(screen.getByText('lux@1.4.2')).toBeTruthy();
    expect(screen.queryByText(/No execution node observed/i)).toBeNull();
  });

  it('PROOF 10 — node visible, account unavailable, both stated', () => {
    setFleet({ nodes: [node()], nodeStatus: 'current', status: 'unavailable' });
    renderFleet();
    const summary = screen.getByTestId('fleet-summary').textContent!;
    expect(summary).toMatch(/1 node reporting/);
    expect(summary).toMatch(/account source unavailable/);
    expect(screen.getByTestId('node-mt5-vps-node-1').textContent)
      .toMatch(/has not reported an MT5 terminal observation/i);
  });

  it('PROOF 6 — a stale node stays visible AND is labelled stale', () => {
    setFleet({
      nodes: [node({ lifecycleState: 'stale',
                     warnings: ['node telemetry stale — last reported, not current'] })],
      nodeStatus: 'stale',
    });
    renderFleet();
    expect(screen.getByText('vps-node-1')).toBeTruthy();
    expect(screen.getByTestId('node-lifecycle-vps-node-1').textContent).toBe('stale');
    expect(screen.getByTestId('node-warnings-vps-node-1').textContent)
      .toMatch(/last reported, not current/);
  });

  it('PROOF 7 — a degraded node stays visible AND names its failure', () => {
    setFleet({
      nodes: [node({ lifecycleState: 'degraded',
                     degradedReasons: ['node cycle status: error'],
                     warnings: ['node cycle status: error'] })],
      nodeStatus: 'degraded',
    });
    renderFleet();
    expect(screen.getByText('vps-node-1')).toBeTruthy();
    expect(screen.getByTestId('node-lifecycle-vps-node-1').textContent).toBe('degraded');
    expect(screen.getByTestId('node-warnings-vps-node-1').textContent)
      .toMatch(/node cycle status: error/);
  });

  it('PROOF 9 — no node observed renders a stated absence, not "0 nodes"', () => {
    setFleet({ nodeStatus: 'absent' });
    renderFleet();
    // Stated in the summary line AND in the empty state.
    expect(screen.getAllByText(/No execution node observed/i).length).toBeGreaterThan(0);
    // Not "0 nodes": nothing was observed, so there is nothing to count.
    expect(screen.getByTestId('fleet-summary').textContent).not.toMatch(/\d/);
    expect(screen.getByText(/not evidence that a node has stopped/i)).toBeTruthy();
  });

  it('PROOFS 13–14 — two nodes keep distinct identities and states', () => {
    setFleet({
      nodes: [node({ nodeId: 'vps-a' }),
              node({ nodeId: 'vps-b', lifecycleState: 'degraded',
                     warnings: ['node froze its execution cycle'] })],
      nodeStatus: 'degraded',
    });
    renderFleet();
    expect(screen.getByText('vps-a')).toBeTruthy();
    expect(screen.getByText('vps-b')).toBeTruthy();
    expect(screen.getByTestId('node-lifecycle-vps-a').textContent).toBe('current');
    expect(screen.getByTestId('node-lifecycle-vps-b').textContent).toBe('degraded');
  });

  it('PROOF 11 — pending orders render unknown, never zero', () => {
    setFleet({ nodes: [node({ openPositionCount: 0, openOrderCount: null })],
               nodeStatus: 'current' });
    const { container } = renderFleet();
    // A genuine empty mirror IS a measured zero and shows as 0.
    expect(container.textContent).toMatch(/Node open positions/);
    // Pending-order exposure has no counterpart in v1 and is not on the card
    // at all — it is neither shown as 0 nor invented.
    expect(container.textContent).not.toMatch(/pending orders\s*0/i);
  });

  it('PROOFS 12 & 18 — no broker or fixture value can appear on a node card', () => {
    setFleet({ nodes: [node()], nodeStatus: 'current' });
    const { container } = renderFleet();
    const html = container.innerHTML;
    for (const forbidden of ['100,000', '100000', '100412', 'InTrade', 'FIXTURE',
                             'Balance', 'Equity', 'Margin', 'drawdown', 'Armed']) {
      expect(html, `node card shows ${forbidden}`).not.toContain(forbidden);
    }
    expect(html).not.toMatch(/\$\s?[\d,]/);
  });

  it('PROOF 24 — an unreported flag renders "—", never the negative branch', () => {
    setFleet({
      nodes: [node({ killSwitchActive: null, submissionDisabled: null,
                     engineVersion: null, cycleStatus: null })],
      nodeStatus: 'current',
    });
    const { container } = renderFleet();
    expect(container.textContent).toContain('—');
    expect(container.textContent).not.toMatch(/inactive/);
    expect(container.textContent).not.toMatch(/enabled/);
  });

  it('PROOF 23 — engine identity appears only when the node reported it', () => {
    setFleet({ nodes: [node({ engineVersion: null })], nodeStatus: 'current' });
    const { container } = renderFleet();
    expect(container.textContent).not.toContain('lux@');
  });

  it('an explicit unreachable observation is shown as the node stated it', () => {
    setFleet({ nodes: [node({ mt5Observation: 'unreachable' })], nodeStatus: 'current' });
    renderFleet();
    expect(screen.getByTestId('node-mt5-vps-node-1').textContent)
      .toMatch(/could not read its broker position snapshot/i);
  });

  it('PROOF 16 — the card renders the freshness verdict, never recomputes one', () => {
    setFleet({ nodes: [node({ livenessAgeSeconds: 12, freshnessBasis: 'received_at' })],
               nodeStatus: 'current' });
    const { container } = renderFleet();
    // The basis is displayed, so a reader can see WHICH clock decided.
    expect(container.textContent).toMatch(/12s \(by received_at\)/);
  });

  it('PROOF 22 — a legacy node is labelled, not silently trusted', () => {
    setFleet({ nodes: [node({ legacySource: true })], nodeStatus: 'current' });
    renderFleet();
    expect(screen.getByTestId('node-legacy-vps-node-1').textContent)
      .toMatch(/predates the versioned telemetry contract/i);
  });
});
