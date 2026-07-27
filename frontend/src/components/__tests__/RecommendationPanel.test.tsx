/**
 * LIVE-4D — the Recommendation UI renders recorded proposals and nothing else.
 *
 *   - the table and detail view print backend-recorded values verbatim;
 *   - an unavailable term renders as "—", NEVER as 0 (no synthesised terms);
 *   - status, conflict, linkage and past-due states appear as explicit badges
 *     driven by the projection, never reconstructed in the browser;
 *   - operator identity is displayed only as the pseudonym the backend sent;
 *   - there are NO accept / reject / execute controls — acceptance is not a
 *     browser capability in this slice.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type {
  RecommendationDecisionView,
  RecommendationListView,
  RecommendationOperationalSummary,
  RecommendationOperationalView,
} from '@/lib/api';
import { api } from '@/lib/api';
import { RecommendationPanel } from '@/components/domain/RecommendationPanel';

function decision(
  over: Partial<RecommendationDecisionView> = {},
): RecommendationDecisionView {
  return {
    decisionId: 'dec_1', decisionType: 'ACCEPT', actorType: 'OPERATOR',
    actor: 'actor_9f2a1b7c4d5e', occurredAt: '2026-07-27T12:02:00Z', sequence: 2,
    reason: 'conditions met', authorizationReference: 'auth_ref_1',
    executionMode: 'observe', ...over,
  };
}

function rec(
  over: Partial<RecommendationOperationalView> = {},
): RecommendationOperationalView {
  return {
    recommendationId: 'rcm_aaaaaaaaaaaaaaaa', scenarioId: 'scn_bbbbbbbbbbbbbbbb',
    instrument: 'EURUSD', direction: 'long', source: 'OPERATOR', status: 'ACCEPTED',
    outcome: 'NOT_EXECUTED', latestDecision: decision(), actorType: 'OPERATOR',
    proposedEntry: 1.10000, stopLoss: 1.09500, takeProfit: 1.11000, quantity: 0.5,
    riskAmount: 25, riskPercent: 0.5, plannedR: 2, rationale: 'BOS retest',
    confidence: 0.72, linkedIntentCount: 1, linkedIntentIds: ['intent_1'],
    supersededBy: null, supersedes: null, createdAt: '2026-07-27T12:00:00Z',
    expiresAt: '2026-07-27T18:00:00Z', ageSeconds: 3600, pastDue: false,
    active: true, nodeId: 'node-1', accountFingerprintMasked: 'acct…fp_1',
    executionTermsAvailability: 'ok', riskTermsAvailability: 'ok',
    warnings: [], tags: [], provenance: 'durable-store', freshness: null, ...over,
  };
}

function summary(
  over: Partial<RecommendationOperationalSummary> = {},
): RecommendationOperationalSummary {
  return {
    activeCount: 1, pendingDecisionCount: 0, acceptedCount: 1, rejectedCount: 0,
    expiredCount: 0, withdrawnCount: 0, supersededCount: 0, executionLinkedCount: 1,
    latestCreatedAt: '2026-07-27T12:00:00Z', availability: 'ok',
    provenance: 'durable-store', freshness: null, ...over,
  };
}

function view(over: Partial<RecommendationListView> = {}): RecommendationListView {
  return {
    recommendations: [rec()], summary: summary(), totalCount: 1, limit: 50,
    offset: 0, ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecommendationPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('RecommendationPanel', () => {
  it('renders the proposal table from recorded values', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('recommendation-table'));
    const row = screen.getByTestId('recommendation-row').textContent ?? '';
    expect(row).toContain('EURUSD');
    expect(row).toContain('long');
    expect(row).toContain('OPERATOR');
    expect(row).toContain('1.10000');      // proposed entry, recorded precision
    expect(row).toContain('1.09500');      // stop
    expect(row).toContain('1.11000');      // target
    expect(row).toContain('0.50');         // quantity
    expect(row).toContain('2R');           // planned R, verbatim — not computed
    expect(row).toContain('ACCEPT');       // latest decision type
  });

  it('marks the panel read-only and acceptance non-executing', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('recommendation-scope'));
    const scope = screen.getByTestId('recommendation-scope').textContent ?? '';
    expect(scope).toContain('READ ONLY');
    expect(scope).toContain('PROPOSAL ONLY');
    expect(scope).toContain('ACCEPTANCE DOES NOT EXECUTE');
  });

  it('renders unavailable terms as dashes, never as zero', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view({
      recommendations: [rec({
        proposedEntry: null, stopLoss: null, takeProfit: null, quantity: null,
        riskAmount: null, riskPercent: null, plannedR: null, ageSeconds: null,
        executionTermsAvailability: 'unavailable', riskTermsAvailability: 'unavailable',
      })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('recommendation-row'));
    const row = screen.getByTestId('recommendation-row').textContent ?? '';
    expect(row).toContain('—');
    expect(row).not.toContain('0.00');
    expect(row).not.toContain('0.00000');
    expect(row).not.toContain('0R');
  });

  it('shows an ACCEPTED badge without implying execution', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('recommendation-badges'));
    const badges = screen.getByTestId('recommendation-badges').textContent ?? '';
    expect(badges).toContain('ACCEPTED');
    expect(badges).not.toContain('EXECUTED');
  });

  it('badges scenario-unavailable, conflicted, unlinked and past-due states', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view({
      recommendations: [rec({
        status: 'PENDING_DECISION', linkedIntentCount: 0, linkedIntentIds: [],
        pastDue: true,
        warnings: ['scenario_unavailable', 'instrument_differs_from_scenario'],
      })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('recommendation-badges'));
    const badges = screen.getByTestId('recommendation-badges').textContent ?? '';
    expect(badges).toContain('PENDING DECISION');
    expect(badges).toContain('SCENARIO UNAVAILABLE');
    expect(badges).toContain('INTENT UNLINKED');
    expect(badges).toContain('CONFLICTED');
    expect(badges).toContain('PAST DUE');
  });

  it('renders proposal counts without performance analytics', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('recommendation-summary'));
    const s = screen.getByTestId('recommendation-summary').textContent ?? '';
    expect(s).toContain('Active');
    expect(s).toContain('Pending decision');
    expect(s).toContain('Intent linked');
    for (const banned of ['Win rate', 'Expectancy', 'Drawdown', 'Equity']) {
      expect(s).not.toContain(banned);
    }
  });

  it('opens a detail view showing lineage and decision history', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    vi.spyOn(api, 'tradeRecommendation').mockResolvedValue({
      ...rec(),
      decisions: [decision({ decisionId: 'dec_0', decisionType: 'DEFER', sequence: 1,
                             reason: 'awaiting session open' }), decision()],
      history: [{ eventId: 'evt_1', recommendationId: 'rcm_aaaaaaaaaaaaaaaa',
                  sequence: 1, eventType: 'CREATED', occurredAt: '2026-07-27T12:00:00Z',
                  recordedAt: '2026-07-27T12:00:00Z', payload: {},
                  provenance: 'durable-store' }],
    });
    mount();
    await waitFor(() => screen.getByTestId('recommendation-row'));
    fireEvent.click(screen.getByTestId('recommendation-row'));
    await waitFor(() => expect(
      screen.getByTestId('recommendation-decisions').textContent).toContain('DEFER'));
    const lineage = screen.getByTestId('recommendation-lineage').textContent ?? '';
    expect(lineage).toContain('scn_bbbbbbbbbbbbbbbb');
    expect(lineage).toContain('intent_1');
    expect(lineage).toContain('acct…fp_1');
    const decisions = screen.getByTestId('recommendation-decisions').textContent ?? '';
    expect(decisions).toContain('DEFER');
    expect(decisions).toContain('ACCEPT');
    expect(decisions).toContain('awaiting session open');
    const history = screen.getByTestId('recommendation-history').textContent ?? '';
    expect(history).toContain('CREATED');
  });

  it('shows only the pseudonymized operator identity', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    vi.spyOn(api, 'tradeRecommendation').mockResolvedValue({
      ...rec(), decisions: [decision()], history: [],
    });
    const { container } = mount();
    await waitFor(() => screen.getByTestId('recommendation-row'));
    fireEvent.click(screen.getByTestId('recommendation-row'));
    await waitFor(() => expect(
      screen.getByTestId('recommendation-decisions').textContent)
        .toContain('actor_9f2a1b7c4d5e'));
    const text = container.textContent ?? '';
    expect(text).not.toContain('op_1');
  });

  it('filters without recomputing any recorded value', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view({
      recommendations: [rec(), rec({ recommendationId: 'rcm_cccccccccccccccc',
                                     instrument: 'GBPUSD', status: 'REJECTED' })],
      totalCount: 2,
    }));
    mount();
    await waitFor(() => screen.getByTestId('recommendation-table'));
    expect(screen.getAllByTestId('recommendation-row')).toHaveLength(2);
    fireEvent.change(screen.getByTestId('recommendation-filter-instrument'),
                     { target: { value: 'GBPUSD' } });
    await waitFor(() =>
      expect(screen.getAllByTestId('recommendation-row')).toHaveLength(1));
    expect(screen.getByTestId('recommendation-row').textContent).toContain('GBPUSD');
    fireEvent.change(screen.getByTestId('recommendation-filter-link'),
                     { target: { value: 'unlinked' } });
    await waitFor(() => screen.getByTestId('recommendation-empty'));
  });

  it('distinguishes an empty store from an unreadable one', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view({
      recommendations: [], totalCount: 0, summary: summary({ activeCount: 0 }),
    }));
    const empty = mount();
    await waitFor(() => screen.getByTestId('recommendation-empty'));
    expect(screen.getByTestId('recommendation-empty').textContent)
      .toContain('none are generated automatically');
    empty.unmount();

    vi.spyOn(api, 'tradeRecommendations').mockRejectedValue(new Error('boom'));
    mount();
    await waitFor(() => screen.getByTestId('recommendation-error'));
    const err = screen.getByTestId('recommendation-error').textContent ?? '';
    expect(err).toContain('UNAVAILABLE');
    expect(err).toContain('not a claim that no proposals exist');
  });

  it('offers no decision, execution or edit controls', async () => {
    vi.spyOn(api, 'tradeRecommendations').mockResolvedValue(view());
    const { container } = mount();
    await waitFor(() => screen.getByTestId('recommendation-table'));
    // Read-only means no control affordances. The word "ACCEPTED" is legitimate
    // status vocabulary, so this asserts the absence of CONTROLS and of any
    // client-side write capability — not the absence of the vocabulary.
    expect(container.querySelectorAll('button')).toHaveLength(0);
    expect(container.querySelectorAll('form')).toHaveLength(0);
    expect(container.querySelectorAll('input')).toHaveLength(0);
    expect(container.querySelectorAll('[role="button"]')).toHaveLength(0);
    // The only interactive elements are the read-only filter selects.
    expect(container.querySelectorAll('select').length)
      .toBe(screen.getByTestId('recommendation-filters').querySelectorAll('select').length);
    for (const write of ['acceptTradeRecommendation', 'rejectTradeRecommendation',
                         'withdrawTradeRecommendation', 'createTradeRecommendation',
                         'decideTradeRecommendation']) {
      expect((api as unknown as Record<string, unknown>)[write]).toBeUndefined();
    }
  });
});
