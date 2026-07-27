/**
 * LIVE-4B — the Scenario section renders projected scenarios and nothing else.
 *
 *   - scenario rows show identity, lifecycle status, natural key and linkage
 *     counts exactly as projected;
 *   - status and freshness badges are explicit;
 *   - filtering by status / instrument / session / node is a pure view concern;
 *   - an unreadable store is UNAVAILABLE, never "no scenarios";
 *   - the panel offers NO editing, mutation or strategy control.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ProjectionFreshness, ScenarioOperationalView, ScenarioSummaryView } from '@/lib/api';
import { api } from '@/lib/api';
import { ScenarioPanel } from '@/components/domain/ScenarioPanel';

function fresh(over: Partial<ProjectionFreshness> = {}): ProjectionFreshness {
  return {
    projectionAt: '2026-07-27T12:05:00Z', sourceAt: '2026-07-27T12:00:00Z',
    ageSeconds: 300, stale: false, available: true, staleAfterSeconds: 120,
    status: 'ok', detail: null, ...over,
  };
}

function scenario(over: Partial<ScenarioOperationalView> = {}): ScenarioOperationalView {
  return {
    scenarioId: 'scn_eac566e01b053f41', instrument: 'EURUSD', direction: 'long',
    session: 'london', structure: 'BOS', entryModel: 'BullExpand', timeframe: 'M15',
    status: 'QUALIFIED', outcome: 'PENDING', nodeId: 'node-1',
    accountFingerprintMasked: 'acct…ive1', linkedRecommendation: 'rec_1',
    linkedIntentCount: 2, linkedOrderCount: 1, linkedPositionCount: 1,
    createdAt: '2026-07-27T12:00:00Z', updatedAt: '2026-07-27T12:01:00Z',
    expiresAt: null, ageSeconds: 300, active: true, tags: [],
    provenance: 'durable-store', freshness: fresh(), ...over,
  };
}

function summary(over: Partial<ScenarioSummaryView> = {}): ScenarioSummaryView {
  return { total: 1, active: 1, byStatus: { QUALIFIED: 1 },
           byInstrument: { EURUSD: 1 }, byOutcome: { PENDING: 1 }, ...over };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ScenarioPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('ScenarioPanel', () => {
  it('renders projected scenarios with identity, lifecycle and linkage counts', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({ scenarios: [scenario()], summary: summary() });
    mount();
    await waitFor(() => screen.getByTestId('scenario-table'));
    const row = screen.getByTestId('scenario-row').textContent ?? '';
    expect(row).toContain('scn_eac566e0');       // truncated identity
    expect(row).toContain('QUALIFIED');
    expect(row).toContain('EURUSD');
    expect(row).toContain('long');
    expect(row).toContain('london');
    expect(row).toContain('BOS');
    expect(row).toContain('BullExpand');
    expect(row).toContain('node-1');
    expect(row).toContain('rec_1');
    expect(screen.getByTestId('scenario-panel').textContent).toContain('1 active / 1');
  });

  it('marks the panel read-only and the parent of every trade', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({ scenarios: [], summary: summary({ total: 0, active: 0 }) });
    mount();
    await waitFor(() => screen.getByTestId('scenario-scope'));
    const scope = screen.getByTestId('scenario-scope').textContent ?? '';
    expect(scope).toContain('READ ONLY');
    expect(scope).toContain('PARENT OF EVERY TRADE');
  });

  it('shows an active status badge and a terminal one differently', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({
      scenarios: [scenario(), scenario({ scenarioId: 'scn_ffffffffffffffff',
                                         status: 'CANCELLED', active: false })],
      summary: summary({ total: 2 }),
    });
    mount();
    await waitFor(() => screen.getByTestId('scenario-table'));
    const badges = screen.getAllByTestId('scenario-status-badge').map((b) => b.textContent);
    expect(badges).toContain('QUALIFIED');
    expect(badges).toContain('CANCELLED');
  });

  it('shows a STALE freshness badge when the projection is stale', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({
      scenarios: [scenario({ freshness: fresh({ stale: true, status: 'stale' }) })],
      summary: summary(),
    });
    mount();
    await waitFor(() => screen.getByTestId('scenario-freshness-badge'));
    expect(screen.getByTestId('scenario-freshness-badge').textContent).toBe('STALE');
  });

  it('filters by status, instrument, session and node', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({
      scenarios: [
        scenario(),
        scenario({ scenarioId: 'scn_1111111111111111', instrument: 'GBPUSD',
                   session: 'asia', status: 'WATCHING', nodeId: 'node-2' }),
      ],
      summary: summary({ total: 2, active: 2 }),
    });
    mount();
    await waitFor(() => screen.getByTestId('scenario-table'));
    expect(screen.getAllByTestId('scenario-row')).toHaveLength(2);

    fireEvent.change(screen.getByTestId('scenario-filter-instrument'), { target: { value: 'GBPUSD' } });
    expect(screen.getAllByTestId('scenario-row')).toHaveLength(1);
    expect(screen.getByTestId('scenario-row').textContent).toContain('GBPUSD');

    fireEvent.change(screen.getByTestId('scenario-filter-instrument'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('scenario-filter-status'), { target: { value: 'WATCHING' } });
    expect(screen.getAllByTestId('scenario-row')).toHaveLength(1);

    fireEvent.change(screen.getByTestId('scenario-filter-status'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('scenario-filter-session'), { target: { value: 'asia' } });
    expect(screen.getAllByTestId('scenario-row')).toHaveLength(1);

    fireEvent.change(screen.getByTestId('scenario-filter-session'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('scenario-filter-node'), { target: { value: 'node-2' } });
    expect(screen.getAllByTestId('scenario-row')).toHaveLength(1);
  });

  it('explains an empty store without claiming scenarios cannot exist', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({
      scenarios: [], summary: summary({ total: 0, active: 0, byStatus: {}, byInstrument: {}, byOutcome: {} }),
    });
    mount();
    await waitFor(() => screen.getByTestId('scenario-empty'));
    expect(screen.getByTestId('scenario-empty').textContent)
      .toContain('created explicitly');
  });

  it('shows UNAVAILABLE when the scenario store cannot be read', async () => {
    vi.spyOn(api, 'scenarios').mockRejectedValue(new Error('failed to fetch'));
    mount();
    await waitFor(() => screen.getByTestId('scenario-error'));
    const err = screen.getByTestId('scenario-error').textContent ?? '';
    expect(err).toContain('UNAVAILABLE');
    expect(err).toContain('not a claim that none exist');
    expect(screen.queryByTestId('scenario-table')).toBeNull();
  });

  it('offers no editing, mutation or strategy control', async () => {
    vi.spyOn(api, 'scenarios').mockResolvedValue({ scenarios: [scenario()], summary: summary() });
    mount();
    await waitFor(() => screen.getByTestId('scenario-table'));
    expect(document.querySelectorAll('button').length).toBe(0);
    expect(document.querySelectorAll('input, textarea').length).toBe(0);
    // Only the four read-only filter selects exist.
    expect(document.querySelectorAll('select').length).toBe(4);
  });
});
