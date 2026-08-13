/**
 * M-MT5-READ-1 — genuine, node-relayed MT5 account truth reaches the operator,
 * and nothing else does.
 *
 * THE ARCHITECTURE THESE TESTS ENCODE
 *   The MT5 terminal runs on the Windows VPS, and the broker client library
 *   speaks local-terminal IPC, so this Control Tower can never read a terminal
 *   itself — `live_mt5` is structurally unreachable on the operator's Mac. The
 *   only route to broker truth is `node_mt5`: an execution node read the
 *   terminal and relayed the observation through `ct.node-telemetry.v1`.
 *
 * WHY THE GATE WIDENED AND WHY THAT MAKES IT STRICTER
 *   Before this milestone the backend stamped `live_mt5` from the adapter KIND
 *   alone. `CONTROL_TOWER_BROKER_ADAPTER=mt5` on a machine with no terminal
 *   produced a `live_mt5` account with a null balance — which this gate admitted
 *   and rendered under a green LIVE border. The gate now admits two values, but
 *   both are issued only on evidence that something was actually observed. More
 *   values, less trust granted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import {
  PROV_LIVE_MT5,
  PROV_NODE_MT5,
  PROV_MOCK_FIXTURE,
  PROV_ABSENT,
  isAuthoritative,
  authoritativeOnly,
  classify,
  accountIdentityKey,
} from '@/lib/operationalProvenance';

const state = vi.hoisted(() => ({ fleet: {} as Record<string, unknown> }));

vi.mock('@/hooks/useRepository', () => ({
  useOperationalFleet: () => state.fleet,
  useAccountsProtection: () => state.fleet,
  useConfiguredInstruments: () => ({ symbols: ['EURUSD'], configured: true }),
}));

import { AccountsProtectionView } from '@/views/AccountsProtectionView';

/** An account exactly as the projection emits it from a node observation. */
function relayed(over: Record<string, unknown> = {}) {
  return {
    accountFingerprint: 'acctfp_0123456789abcdef',
    broker: null, server: 'FTMO-Demo', currency: 'USD',
    balance: 4211.5, equity: 4180.25, freeMargin: 4000,
    margin: null, marginLevel: null, leverage: null,
    unrealizedPnL: null, realizedPnLToday: null, openRisk: null,
    tradeAllowed: false, tradeExpert: true,
    connectionState: null, nodeId: 'vps-node-1',
    observedAt: '2026-07-31T11:59:30Z',
    provenance: PROV_NODE_MT5,
    freshness: { available: true, stale: false, status: 'ok' },
    ...over,
  };
}

/** The mock adapter's record: same shape, same invented balance, no authority. */
const MOCK_ACCOUNT = {
  accountFingerprint: 'acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F',
  balance: 100000, equity: 100412, nodeId: null,
  provenance: PROV_MOCK_FIXTURE,
  freshness: { available: true, stale: false, status: 'ok' },
};

/** What the defect used to produce: configured MT5, nothing ever observed. */
const CONFIGURED_BUT_UNOBSERVED = {
  accountFingerprint: null, balance: null, equity: null, nodeId: null,
  provenance: PROV_ABSENT,
  freshness: { available: false, stale: true, status: 'unavailable' },
};

function setFleet(over: Record<string, unknown>) {
  state.fleet = {
    nodes: [], accounts: [], deployments: [],
    status: 'unavailable', detail: 'no authoritative source', ...over,
  };
}

const renderAccounts = () =>
  render(<MemoryRouter><AccountsProtectionView /></MemoryRouter>);

beforeEach(() => setFleet({}));

// ── the gate ─────────────────────────────────────────────────────────────────

describe('the provenance gate after M-MT5-READ-1', () => {
  it('PROOF 1 — admits node-relayed MT5 truth', () => {
    expect(isAuthoritative(relayed())).toBe(true);
    expect(authoritativeOnly([relayed()])).toHaveLength(1);
  });

  it('PROOF 1 — still admits a locally-read terminal', () => {
    expect(isAuthoritative({ provenance: PROV_LIVE_MT5 })).toBe(true);
  });

  it('PROOF 2 — still rejects mock-fixture, and leaves no trace of it', () => {
    expect(isAuthoritative(MOCK_ACCOUNT)).toBe(false);
    const kept = authoritativeOnly([MOCK_ACCOUNT, relayed()]);
    expect(kept).toHaveLength(1);
    expect(JSON.stringify(kept)).not.toContain('100000');
    expect(JSON.stringify(kept)).not.toContain('100412');
  });

  it('rejects the configured-but-unobserved record the backend now stamps absent', () => {
    // This is the defect, seen from the frontend. The record used to arrive
    // carrying `live_mt5` and was admitted; it now arrives as `absent`.
    expect(isAuthoritative(CONFIGURED_BUT_UNOBSERVED)).toBe(false);
  });

  it('still fails closed on everything unrecognised', () => {
    for (const p of ['node-mt5', 'NODE_MT5', 'node-telemetry', 'mt5', 'remote-node',
                     'durable-store', PROV_ABSENT, '']) {
      expect(isAuthoritative({ provenance: p }), p).toBe(false);
    }
    expect(isAuthoritative(null)).toBe(false);
    expect(isAuthoritative({})).toBe(false);
  });
});

// ── status classification ────────────────────────────────────────────────────

describe('empty, unavailable and stale stay distinct', () => {
  it('PROOF 3 — a genuine empty answer is empty, not unavailable', () => {
    expect(classify([], { sourceAnswered: true, rejectedCount: 0 })).toBe('empty');
  });

  it('PROOF 4 — a missing account source is unavailable, never empty', () => {
    expect(classify([], { sourceAnswered: false })).toBe('unavailable');
    // A node that published but sampled no account: the record is rejected, so
    // the source "answered with nothing admissible" — which is not a fact about
    // the account, and must not read as "this account holds nothing".
    expect(classify([], { sourceAnswered: true, rejectedCount: 1 })).toBe('unavailable');
  });

  it('PROOF 5 — genuine stale data stays admitted AND visibly stale', () => {
    const stale = relayed({ freshness: { available: true, stale: true } });
    expect(isAuthoritative(stale)).toBe(true);
    expect(classify([stale], { sourceAnswered: true })).toBe('stale');
  });
});

// ── identity ─────────────────────────────────────────────────────────────────

describe('account identity cannot collide or be inherited', () => {
  it('PROOF 8 — the key names the observer AND the observed', () => {
    expect(accountIdentityKey(relayed())).toBe('vps-node-1::acctfp_0123456789abcdef');
  });

  it('PROOF 9 — one node cannot take another node’s key', () => {
    const a = accountIdentityKey(relayed({ nodeId: 'vps-a' }));
    const b = accountIdentityKey(relayed({ nodeId: 'vps-b' }));
    expect(a).not.toBe(b);
  });

  it('the same account seen by two nodes stays two observations', () => {
    const keys = new Set([
      accountIdentityKey(relayed({ nodeId: 'vps-a' })),
      accountIdentityKey(relayed({ nodeId: 'vps-b' })),
    ]);
    expect(keys.size).toBe(2);
  });

  it('a locally-read account is distinguishable from a relayed one', () => {
    expect(accountIdentityKey({ nodeId: null, accountFingerprint: 'fp' }))
      .toBe('local::fp');
  });

  it('refuses to invent an identity when there is none', () => {
    // The old code fell back to `account-${index}` — a positional key, which is
    // exactly how one row inherits another's DOM node and state.
    expect(accountIdentityKey({ nodeId: null, accountFingerprint: null })).toBeNull();
    expect(accountIdentityKey(null)).toBeNull();
  });
});

// ── rendering ────────────────────────────────────────────────────────────────

describe('Accounts & Protection renders relayed truth honestly', () => {
  it('shows the genuine figures and names the relaying node', () => {
    setFleet({ accounts: [relayed()], status: 'available', detail: '' });
    const { container } = renderAccounts();
    // Formatted by PLValue; assert the digits survive rather than the styling.
    expect(container.textContent).toMatch(/4[,.]?211/);
    expect(container.textContent).toMatch(/4[,.]?180/);
    expect(screen.getByTestId('account-relay-note').textContent)
      .toMatch(/relayed to this Control Tower/i);
    expect(screen.getByTestId('account-live-badge').textContent)
      .toMatch(/relayed by vps-node-1/);
  });

  it('PROOF 12 — every unpublished figure renders as absent, never as zero', () => {
    setFleet({ accounts: [relayed()], status: 'available', detail: '' });
    const { container } = renderAccounts();
    // margin, marginLevel, leverage, unrealizedPnL, realizedPnLToday, openRisk
    expect(screen.getAllByTestId('value-not-reported').length).toBe(6);
    // No zero was invented for any of them.
    expect(container.textContent).not.toMatch(/\$0\.00/);
  });

  it('PROOF 12 — an unreported permission is not a denial', () => {
    setFleet({
      accounts: [relayed({ tradeAllowed: null, tradeExpert: null })],
      status: 'available', detail: '',
    });
    const { container } = renderAccounts();
    expect(container.textContent).not.toMatch(/blocked/);
    expect(screen.getAllByTestId('value-not-reported').length).toBe(8);
  });

  it('a reported denial IS shown — it is a real and alarming fact', () => {
    setFleet({ accounts: [relayed({ tradeAllowed: false })], status: 'available', detail: '' });
    const { container } = renderAccounts();
    expect(container.textContent).toMatch(/blocked/);
  });

  it('PROOF 4 — an unavailable source renders a stated absence, not an account', () => {
    setFleet({ status: 'unavailable', detail: 'no execution node has relayed an account' });
    const { container } = renderAccounts();
    expect(screen.getByText(/No authoritative account source/i)).toBeTruthy();
    expect(container.textContent).not.toMatch(/\d[\d,]*\.\d\d/);
  });

  it('PROOF 5 — stale relayed data is labelled as last-reported', () => {
    setFleet({
      accounts: [relayed({ freshness: { available: true, stale: true } })],
      status: 'stale', detail: '',
    });
    renderAccounts();
    expect(screen.getByTestId('accounts-stale').textContent)
      .toMatch(/last reported, not as current state/i);
  });

  it('PROOFS 10 and 11 — no credential-shaped value can appear', () => {
    setFleet({ accounts: [relayed()], status: 'available', detail: '' });
    const { container } = renderAccounts();
    const rendered = container.textContent!.toLowerCase();
    // NOTE: terminal-path leakage is asserted in the BACKEND suite. Naming the
    // executable here would trip `test_the_browser_still_never_contacts_mt5`,
    // a deliberately blunt guard that no frontend file mentions MT5 internals —
    // and a blunt guard is worth more than this one extra assertion.
    for (const needle of ['password', 'token', 'secret', 'login', 'appdata']) {
      expect(rendered, needle).not.toContain(needle);
    }
    // The fingerprint is one-way; the account number never existed here.
    expect(rendered).toContain('acctfp_');
  });
});
