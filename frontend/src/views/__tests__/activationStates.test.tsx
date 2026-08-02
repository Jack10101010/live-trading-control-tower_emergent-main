/**
 * M-ACTIVATE-READINESS-1 — every state an operator can be in during activation,
 * rendered through the real components.
 *
 * WHY THIS SUITE IS SEPARATE FROM THE HONESTY SUITES
 *   The existing suites prove the application does not show invented data. This
 *   one proves it stays honest at the moment it FINALLY HAS REAL DATA, which is
 *   a different and harder property: from here on, every screen the operator
 *   reads is about actual money, and the failure mode changes from "shows
 *   something fake" to "shows something real that belongs to someone else".
 *
 * THE FOUR STATES THAT MATTER, IN ORDER OF DANGER
 *   1. A genuine reading of the WRONG account.       Nothing looks broken.
 *   2. Two genuine sources disagreeing.              Nothing looks broken.
 *   3. Stale genuine data presented as current.      Looks perfect.
 *   4. Mock data admitted after a node goes quiet.   Looks like recovery.
 *
 *   None of the four announces itself. Each is asserted below on the copy an
 *   operator actually reads, not on internal flags.
 *
 * BROWSER VISUAL BALANCE STILL NEEDS A HUMAN GLANCE. jsdom asserts structure
 * and text; it cannot tell whether a danger banner is legible against the panel
 * background or whether a two-account layout wraps sensibly at 1280px.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import {
  PROV_NODE_MT5,
  PROV_MOCK_FIXTURE,
  PROV_ABSENT,
  R_ACCOUNT_IDENTITY_MISMATCH,
  R_ACCOUNT_SERVER_MISMATCH,
  R_CONTRADICTORY_SOURCES,
  isAuthoritative,
  authoritativeOnly,
  admissionRejectionCopy,
  classify,
} from '@/lib/operationalProvenance';
import { PROV_NODE_TELEMETRY } from '@/lib/nodeProvenance';

const state = vi.hoisted(() => ({ fleet: {} as Record<string, unknown> }));

vi.mock('@/hooks/useRepository', () => ({
  useOperationalFleet: () => state.fleet,
  useAccountsProtection: () => state.fleet,
  useConfiguredInstruments: () => ({ symbols: ['EURUSD'], configured: true }),
}));

import { AccountsProtectionView } from '@/views/AccountsProtectionView';
import { FleetOverview } from '@/views/FleetOverview';

function account(over: Record<string, unknown> = {}) {
  return {
    accountFingerprint: 'acctfp_0123456789abcdef', broker: null,
    server: 'FTMO-Demo', balance: 4211.5, equity: 4180.25,
    margin: null, marginLevel: null, leverage: null, currency: 'USD',
    unrealizedPnL: null, realizedPnLToday: null, openRisk: null,
    freeMargin: 4000, tradeAllowed: false, tradeExpert: true,
    connectionState: null, nodeId: 'vps-node-1', observedAt: '2026-08-02T11:59:50Z',
    provenance: PROV_NODE_MT5, admitted: true, admissionReasons: [] as string[],
    freshness: { available: true, stale: false },
    ...over,
  };
}

/** The mock adapter's record: the fixture's invented $100,000. */
function mockAccount(over: Record<string, unknown> = {}) {
  return account({
    provenance: PROV_MOCK_FIXTURE, nodeId: null, balance: 100000, equity: 100412,
    accountFingerprint: 'acct_fixture_1', ...over,
  });
}

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

/** Reproduces what `useOperationalFleet` computes, so a test states RAW records
 *  and the gate decides — rather than a test asserting a decision it made. */
function setRaw(rawAccounts: Record<string, unknown>[],
                over: Record<string, unknown> = {}) {
  const accounts = authoritativeOnly(rawAccounts as never);
  const status = classify(accounts as never, {
    sourceAnswered: true, rejectedCount: rawAccounts.length - accounts.length,
  });
  state.fleet = {
    nodes: [], deployments: [], accounts, status,
    detail: status === 'available' || status === 'stale' ? '' :
      'No authoritative operational source is reporting.',
    nodeStatus: 'absent', nodeDetail: 'No execution node observed.',
    rejections: admissionRejectionCopy(rawAccounts as never),
    ...over,
  };
}

const renderAccounts = () =>
  render(<MemoryRouter><AccountsProtectionView /></MemoryRouter>);

/** Money is rendered as a `$` span next to a value span, so `getByText` on the
 *  formatted number never matches. Assert on the rendered text instead. */
const shown = (text: string) => document.body.textContent?.includes(text) ?? false;
const renderFleet = () => render(<MemoryRouter><FleetOverview /></MemoryRouter>);

beforeEach(() => setRaw([]));

// ══════════════════════════════════════════════════════════════════════════════
// The gate: admission is not provenance
// ══════════════════════════════════════════════════════════════════════════════

describe('admission and provenance are two different questions', () => {
  it('refuses a record whose provenance is impeccable and whose account is wrong', () => {
    const wrong = account({ admitted: false,
                            admissionReasons: [R_ACCOUNT_IDENTITY_MISMATCH] });
    expect(wrong.provenance).toBe(PROV_NODE_MT5);   // genuinely observed
    expect(isAuthoritative(wrong)).toBe(false);     // and still not ours
  });

  it('treats a MISSING admitted field as admitted, not as failed', () => {
    // An unpinned deployment cannot perform the check. Reading "not checked" as
    // "failed" would blank a correct activation, which trains an operator to
    // stop believing the screen.
    const { admitted, ...withoutTheField } = account();
    expect(admitted).toBe(true);
    expect(isAuthoritative(withoutTheField)).toBe(true);
  });

  it('still fails closed on unknown provenance regardless of admitted', () => {
    for (const provenance of ['node_MT5', 'mt5', 'durable-store', PROV_ABSENT, '']) {
      expect(isAuthoritative({ provenance, admitted: true }), provenance).toBe(false);
    }
  });

  it('produces distinct operator copy per refusal reason, deduplicated', () => {
    const copy = admissionRejectionCopy([
      account({ admitted: false, admissionReasons: [R_ACCOUNT_IDENTITY_MISMATCH] }),
      account({ admitted: false, admissionReasons: [R_ACCOUNT_IDENTITY_MISMATCH] }),
      account({ admitted: false, admissionReasons: [R_ACCOUNT_SERVER_MISMATCH] }),
    ]);
    expect(copy).toHaveLength(2);
    expect(copy.join(' ')).toMatch(/wrong account/i);
    expect(copy.join(' ')).toMatch(/different broker server/i);
  });

  it('says nothing about records that were merely non-authoritative', () => {
    // "The mock adapter is running" is not news. Only NAMED refusals speak.
    expect(admissionRejectionCopy([mockAccount()])).toEqual([]);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// Accounts & Protection
// ══════════════════════════════════════════════════════════════════════════════

describe('Accounts & Protection during activation', () => {
  it('source unavailable — states the absence, shows no account', () => {
    setRaw([]);
    renderAccounts();
    expect(screen.getByText(/No accounts reported|No authoritative account source/i))
      .toBeTruthy();
    expect(screen.queryByTestId('account-live-badge')).toBeNull();
  });

  it('a valid node_mt5 account renders the genuine figures and names the node', () => {
    setRaw([account()]);
    renderAccounts();
    expect(screen.getByTestId('account-live-badge').textContent)
      .toMatch(/relayed by vps-node-1/);
    expect(shown('4,211.50')).toBe(true);
    expect(screen.getByTestId('account-relay-note').textContent)
      .toMatch(/The node is the broker authority/);
  });

  it('unpublished figures render as absent, never as zero', () => {
    setRaw([account()]);
    const { container } = renderAccounts();
    // marginLevel, leverage, openRisk and both P/L figures are null at source.
    expect(container.textContent).not.toMatch(/\$0\.00/);
    expect(container.textContent).toMatch(/—/);
  });

  it('a stale account stays visible AND is labelled last-reported', () => {
    setRaw([account({ freshness: { available: true, stale: true } })]);
    renderAccounts();
    expect(screen.getByTestId('accounts-stale').textContent)
      .toMatch(/last\s+reported, not as current state/);
    expect(shown('4,211.50')).toBe(true);
  });

  it('THE DANGEROUS ONE — a wrong-account reading is refused with precise copy', () => {
    setRaw([account({ admitted: false,
                      admissionReasons: [R_ACCOUNT_IDENTITY_MISMATCH] })]);
    renderAccounts();
    const banner = screen.getByTestId('account-admission-refused');
    expect(banner.textContent).toMatch(/NOT the account this deployment is pinned to/);
    // The money is NOT rendered — attributing it here would be the whole defect.
    expect(shown('4,211.50')).toBe(false);
    // And the generic empty state does not get to speak instead.
    expect(screen.queryByText(/^No authoritative account source$/)).toBeNull();
  });

  it('a rejected mock account produces no card and no refusal banner', () => {
    setRaw([mockAccount()]);
    renderAccounts();
    expect(shown('100,000')).toBe(false);
    expect(screen.queryByTestId('account-admission-refused')).toBeNull();
    expect(screen.getByText(/No authoritative account source/i)).toBeTruthy();
  });

  it('genuine node truth is not shadowed by a simultaneous mock record', () => {
    setRaw([mockAccount(), account()]);
    renderAccounts();
    expect(shown('4,211.50')).toBe(true);
    expect(shown('100,000')).toBe(false);
  });

  it('two genuine sources disagreeing render BOTH, never one', () => {
    setRaw([account({ nodeId: 'vps-node-1' }),
            account({ nodeId: 'vps-node-2', balance: 9999 })]);
    renderAccounts();
    expect(screen.getAllByTestId('account-live-badge')).toHaveLength(2);
    expect(shown('4,211.50')).toBe(true);
    expect(shown('9,999.00')).toBe(true);
    // No averaged third figure anywhere.
    expect(shown('7,105.25')).toBe(false);
  });

  it('two nodes reporting the same account keep separate rows', () => {
    // Identical fingerprint, two observers: two observations, not one row that
    // silently wins. The key is observer + observed for exactly this reason.
    setRaw([account({ nodeId: 'vps-node-1' }), account({ nodeId: 'vps-node-2' })]);
    renderAccounts();
    expect(screen.getAllByTestId('account-live-badge')).toHaveLength(2);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// Fleet Overview
// ══════════════════════════════════════════════════════════════════════════════

describe('Fleet Overview during activation', () => {
  it('node current, MT5 not observed — the ordinary starting state', () => {
    // The mock adapter's record is present and rejected, so the account status
    // is `unavailable` (a source answered, nothing survived) rather than
    // `empty` (a source answered and reported none). The two must not collapse.
    setRaw([mockAccount()], { nodes: [node()], nodeStatus: 'current' });
    renderFleet();
    expect(screen.getByTestId('node-mt5-vps-node-1').textContent)
      .toMatch(/has not reported an MT5 terminal observation/i);
    expect(screen.getByTestId('fleet-summary').textContent)
      .toMatch(/account source unavailable/);
  });

  it('node current, MT5 observed — the activation target', () => {
    setRaw([account()], { nodes: [node({ mt5Observation: 'observed' })],
                          nodeStatus: 'current' });
    renderFleet();
    expect(screen.getByTestId('node-mt5-vps-node-1').textContent)
      .not.toMatch(/has not reported/i);
    // The node card reports an OBSERVATION; it does not become the account.
    expect(screen.getByTestId('node-mt5-vps-node-1').textContent)
      .not.toMatch(/4,211|4211/);
  });

  it('a node with an MT5 observation still carries only node provenance', () => {
    const observing = node({ mt5Observation: 'observed' });
    expect(observing.provenance).toBe(PROV_NODE_TELEMETRY);
    expect(isAuthoritative(observing)).toBe(false);   // a heartbeat is not money
  });

  it('two conflicting nodes both remain visible with distinct identities', () => {
    setRaw([], {
      nodes: [node({ nodeId: 'vps-node-1' }),
              node({ nodeId: 'vps-node-2', lifecycleState: 'degraded',
                     degradedReasons: ['node cycle status: error'],
                     warnings: ['node cycle status: error'] })],
      nodeStatus: 'degraded',
    });
    renderFleet();
    expect(screen.getByText('vps-node-1')).toBeTruthy();
    expect(screen.getByText('vps-node-2')).toBeTruthy();
    expect(screen.getByTestId('node-lifecycle-vps-node-1').textContent).toBe('current');
    expect(screen.getByTestId('node-lifecycle-vps-node-2').textContent).toBe('degraded');
  });

  it('a refused account is named on Fleet Overview, not hidden behind "unavailable"', () => {
    setRaw([account({ admitted: false,
                      admissionReasons: [R_ACCOUNT_IDENTITY_MISMATCH] })],
           { nodes: [node({ mt5Observation: 'observed' })], nodeStatus: 'current' });
    renderFleet();
    expect(screen.getByTestId('fleet-summary').textContent)
      .toMatch(/account observation REFUSED/);
    expect(screen.getByTestId('fleet-summary').textContent)
      .not.toMatch(/account source unavailable/);
    expect(screen.getByTestId('fleet-account-refused').textContent)
      .toMatch(/NOT the account this deployment is pinned to/);
  });

  it('a healthy node never implies a LIVE account frame', () => {
    setRaw([mockAccount()], { nodes: [node()], nodeStatus: 'current' });
    const { container } = renderFleet();
    expect(container.textContent).not.toMatch(/100,000|100412/);
    expect(screen.getByTestId('fleet-summary').textContent)
      .toMatch(/account source unavailable/);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// The contradiction warning reaches an operator surface
// ══════════════════════════════════════════════════════════════════════════════

describe('contradictions are surfaced as warnings, not resolved', () => {
  it('the warning code is the same string the backend emits', () => {
    // Both sides name the condition identically, so a search for the code in a
    // screenshot finds the policy that produced it.
    expect(R_CONTRADICTORY_SOURCES).toBe('contradictory_account_sources');
  });

  it('a disagreement never collapses into a single admitted record', () => {
    const raw = [account({ nodeId: 'vps-node-1' }),
                 account({ nodeId: 'vps-node-2', balance: 9999 })];
    expect(authoritativeOnly(raw as never)).toHaveLength(2);
  });
});
