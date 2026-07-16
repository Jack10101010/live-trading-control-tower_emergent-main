import { useOutletContext, useSearchParams } from 'react-router-dom';
import { useEffect, useRef } from 'react';
import {
  useReplaySessionForPair,
  useTrades,
  useMarketState,
  useActivePackage,
} from '@/hooks/useRepository';
import { useShellStore } from '@/store/shellStore';
import { Panel } from '@/components/structures/Panel';
import { FeatureGate } from '@/components/FeatureGate';
import { Badge, MarketStateBadge, PackageVersionChip, HealthDot } from '@/components/primitives';
import { IconButton } from '@/components/primitives/Button';
import { PlayCircle, PauseCircle, SkipBack, SkipForward, Gauge } from 'lucide-react';
import { ChartWorkspace } from '@/components/domain/ChartWorkspace';
import { isoToUnix } from '@/lib/chartData';
import { fmtHash } from '@/lib/format';

const BAR = 900; // 15-min bar in seconds
const TICK_MS = 400;

export function ReplayView() {
  const { pair } = useOutletContext<{ pair: string }>();
  const session = useReplaySessionForPair(pair);
  const { live, ghost, blocked } = useTrades({ pair });
  const marketState = useMarketState(pair);
  const pkg = useActivePackage();
  const openInspector = useShellStore((s) => s.openInspector);

  const startUnix = session ? isoToUnix(session.window.start) : isoToUnix('2026-07-01T08:30:00Z');
  const endUnix = session ? isoToUnix(session.window.end) : isoToUnix('2026-07-01T09:14:00Z');

  // Replay runtime is owned by the shell store (survives route changes) and
  // persisted to localStorage (survives refresh). Cursor/speed/playing all read
  // and write through the store; the clock ticks the store cursor.
  const { cursor, speed, playing } = useShellStore((s) => s.replay);
  const ensureReplayPair = useShellStore((s) => s.ensureReplayPair);
  const setCursor = useShellStore((s) => s.setReplayCursor);
  const setSpeed = useShellStore((s) => s.setReplaySpeed);
  const setPlaying = useShellStore((s) => s.setReplayPlaying);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const [searchParams] = useSearchParams();
  const tParam = Number(searchParams.get('t'));

  // Initialise/realign replay state to this pair's window on mount & pair change.
  useEffect(() => {
    ensureReplayPair(pair, startUnix, endUnix, tParam || undefined);
  }, [pair, startUnix, endUnix, tParam, ensureReplayPair]);

  // Replay clock — advances the virtual cursor; fixture-only, no broker.
  useEffect(() => {
    if (!playing) return;
    timer.current = setInterval(() => {
      const { cursor: c } = useShellStore.getState().replay;
      const next = c + 60 * speed;
      if (next >= endUnix) {
        setPlaying(false);
        setCursor(endUnix);
      } else {
        setCursor(next);
      }
    }, TICK_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [playing, speed, endUnix, setCursor, setPlaying]);

  const clamp = (t: number) => Math.max(startUnix, Math.min(endUnix, t));
  const stepFwd = () => setCursor(clamp(cursor + BAR));
  const stepBack = () => setCursor(clamp(cursor - BAR));

  // Trades that have opened by the cursor; the active one is still open at cursor.
  // (Side-panel state — the chart overlays are now produced by ChartWorkspace layers.)
  const openedByCursor = live.filter((t) => isoToUnix(t.openedAt) <= cursor);
  const activeTrade = openedByCursor.find((t) => !t.closedAt || isoToUnix(t.closedAt) > cursor);

  const cursorPct = endUnix > startUnix ? ((cursor - startUnix) / (endUnix - startUnix)) * 100 : 0;

  return (
    <div className="grid grid-cols-[1fr_360px] h-full min-h-0">
      {/* Chart-dominant primary workspace */}
      <div className="flex flex-col min-h-0">
        {/* Transport */}
        <div
          className="flex items-center gap-3 px-4 h-10 border-b shrink-0"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          <IconButton ariaLabel="Step back" onClick={stepBack} data-testid="replay-step-back">
            <SkipBack size={14} />
          </IconButton>
          <IconButton
            ariaLabel={playing ? 'Pause' : 'Play'}
            onClick={() => setPlaying(!playing)}
            variant="primary"
            data-testid="replay-play"
          >
            {playing ? <PauseCircle size={14} /> : <PlayCircle size={14} />}
          </IconButton>
          <IconButton ariaLabel="Step forward" onClick={stepFwd} data-testid="replay-step-forward">
            <SkipForward size={14} />
          </IconButton>

          <div className="flex items-center gap-1 border-l pl-3 ml-2" style={{ borderColor: 'var(--border-subtle)' }}>
            <Gauge size={12} className="text-text-muted" />
            {[1, 2, 4, 8, 16].map((s) => (
              <button
                key={s}
                onClick={() => setSpeed(s)}
                className={`h-6 px-2 text-2xs rounded-sm ${
                  speed === s
                    ? 'bg-[color:var(--panel-2)] text-text border border-[color:var(--border)]'
                    : 'text-text-2 hover:text-text'
                }`}
              >
                {s}×
              </button>
            ))}
          </div>

          {/* Jump to trade */}
          <select
            className="ml-2 h-6 text-2xs rounded-sm bg-[color:var(--panel-2)] border border-[color:var(--border)] text-text px-1.5 outline-none"
            value=""
            onChange={(e) => {
              const t = live.find((x) => x.tradeId === e.target.value);
              if (t) setCursor(clamp(isoToUnix(t.openedAt)));
            }}
            data-testid="replay-jump-trade"
          >
            <option value="">Jump to trade…</option>
            {live.map((t) => (
              <option key={t.tradeId} value={t.tradeId}>
                {t.scenarioKey.split(':').slice(1).join(':')} · {t.openedAt.slice(11, 16)}
              </option>
            ))}
          </select>

          <div className="ml-auto flex items-center gap-2 text-2xs text-text-muted mono">
            <HealthDot state="warn" size="sm" />
            <span>replay-clock</span>
            <span className="text-text">{new Date(cursor * 1000).toISOString().slice(11, 19)}Z</span>
          </div>
        </div>

        {/* Chart */}
        <div className="flex-1 min-h-0 relative">
          <div className="absolute inset-0">
            <FeatureGate flag="charts">
              <ChartWorkspace
                instrument={pair}
                mode="replay"
                cursor={cursor}
                endISO={session?.window.end}
                count={220}
                volume
              />
            </FeatureGate>
          </div>
        </div>

        {/* Scrubber + jump-to-date */}
        <div
          className="h-14 shrink-0 border-t px-4 py-2 flex flex-col justify-center gap-1"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
        >
          <input
            type="range"
            min={startUnix}
            max={endUnix}
            value={cursor}
            step={60}
            onChange={(e) => setCursor(Number(e.target.value))}
            className="w-full accent-[color:var(--primary)]"
            data-testid="replay-scrubber"
          />
          <div className="flex items-center justify-between text-2xs text-text-muted mono">
            <span>{new Date(startUnix * 1000).toISOString().slice(11, 16)}Z</span>
            <span className="text-text-2">{cursorPct.toFixed(0)}%</span>
            <span>{new Date(endUnix * 1000).toISOString().slice(11, 16)}Z</span>
          </div>
        </div>
      </div>

      {/* Sync panel */}
      <aside
        className="border-l overflow-y-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        <div className="p-4 space-y-4">
          <Panel title="Policy state at cursor" dense>
            <div className="space-y-2 text-xs text-text-2">
              <div className="flex items-center gap-2">
                <PackageVersionChip version={pkg.version} hash={pkg.packageHash} />
              </div>
              {marketState && (
                <MarketStateBadge state={marketState.state} confidence={marketState.confidence} confirmed={marketState.confirmed} />
              )}
              <div className="text-2xs pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                Byte-reproducible — pins <code className="mono">{session ? fmtHash(session.packageHashAtTime) : '—'}</code> and{' '}
                <code className="mono">replayDataVersion</code> from the Deployment Manifest.
              </div>
            </div>
          </Panel>

          <Panel title="Active trade" dense>
            {activeTrade ? (
              <button
                className="w-full text-left rounded-md border p-2 hover:bg-[color:var(--panel-2)] transition-colors"
                style={{ borderColor: 'var(--border-subtle)' }}
                onClick={() => openInspector({ kind: 'trade', tradeId: activeTrade.tradeId })}
                data-testid="replay-active-trade"
              >
                <div className="flex items-center gap-2 mb-1">
                  <Badge variant="status">{activeTrade.state}</Badge>
                  <span className="ml-auto mono text-2xs text-text-muted">{activeTrade.openedAt.slice(11, 16)}Z</span>
                </div>
                <div className="mono text-2xs text-text truncate">{activeTrade.scenarioKey}</div>
                <div className="text-2xs text-text-muted mt-1">
                  Entry {activeTrade.entry} · SL {activeTrade.sl} · TP {activeTrade.tp}
                </div>
              </button>
            ) : (
              <div className="text-xs text-text-muted italic">No open trade at this moment</div>
            )}
          </Panel>

          <Panel title="Trades opened (cumulative)" dense>
            <div className="text-2xs text-text-muted mb-1">
              {openedByCursor.length} live · {ghost.length} ghost · {blocked.length} blocked (window)
            </div>
            <ul className="space-y-1">
              {openedByCursor.map((t) => (
                <li
                  key={t.tradeId}
                  className="flex items-center gap-2 text-2xs cursor-pointer hover:bg-[color:var(--panel-2)] rounded-sm px-1 py-0.5"
                  onClick={() => openInspector({ kind: 'trade', tradeId: t.tradeId })}
                >
                  <span
                    className="w-1.5 h-1.5 rounded-full"
                    style={{ background: t.closedAt && isoToUnix(t.closedAt) <= cursor ? 'var(--text-muted)' : 'var(--live)' }}
                  />
                  <span className="mono text-text truncate">{t.scenarioKey.split(':').slice(1, 4).join(':')}</span>
                  <span className="ml-auto mono text-text-muted">{t.openedAt.slice(11, 16)}</span>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </aside>
    </div>
  );
}
