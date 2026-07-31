import { useFixtureTradesPreview, useFixtureEventsPreview } from '@/hooks/useRepository';
import { Panel } from '@/components/structures/Panel';

/**
 * M-TRADES-1 — the ONLY surface permitted to render fixture trade records.
 *
 * The development fixture holds two live trades with entry/SL/TP and R values,
 * three ghost trades with outcomes, and five blocked intents. Tests and
 * component development still need them; an operator must never see them.
 *
 * Unlinked from navigation, sole importer of `useFixtureTradesPreview`, and
 * unreachable in production where M-ENV-1 never loads the fixture world.
 */
export function FixtureTradesPreview() {
  const { live, ghost, blocked } = useFixtureTradesPreview();
  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-text tracking-tight">Fixture Trades Preview</h1>
        <p
          className="text-xs mt-2 px-2 py-1 rounded border inline-block mono"
          style={{ borderColor: 'var(--mode-mock)', color: 'var(--mode-mock)' }}
          data-testid="fixture-trades-banner"
        >
          DEVELOPMENT FIXTURE — no execution described here ever happened
        </p>
      </div>
      <Panel provenance="fixture" title={`Fixture live trades (${live.length})`}>
        <ul className="text-xs mono space-y-1">
          {live.map((t) => (
            <li key={t.tradeId}>{t.scenarioKey} · {t.state} · {t.lane}</li>
          ))}
        </ul>
      </Panel>
      <Panel provenance="fixture" title={`Fixture ghost trades (${ghost.length})`}>
        <ul className="text-xs mono space-y-1">
          {ghost.map((g) => (
            <li key={g.ghostTradeId}>{g.scenarioKey} · {g.ghostOutcome}</li>
          ))}
        </ul>
      </Panel>
      <FixtureEvents />
      <Panel provenance="fixture" title={`Fixture blocked intents (${blocked.length})`}>
        <ul className="text-xs mono space-y-1">
          {blocked.map((b) => (
            <li key={b.blockedIntentId}>{b.scenarioKey} · {b.blockReason}</li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

/**
 * M-EVENTS-1: the three authored seed events, shown alone. They used to be
 * merged into `/api/events` with no marker, so an operator reading the audit
 * trail could not tell which entries described things that actually happened.
 */
function FixtureEvents() {
  const { events, detail } = useFixtureEventsPreview();
  return (
    <Panel provenance="fixture" title={`Fixture events (${events.length})`}>
      <p className="text-2xs text-text-muted mb-2" data-testid="fixture-events-note">{detail}</p>
      <ul className="text-xs mono space-y-1" data-testid="fixture-events-list">
        {events.map((e) => (
          <li key={e.eventId}>seq {e.seq} · {e.at} · {e.scenarioKey ?? '—'}</li>
        ))}
      </ul>
    </Panel>
  );
}
