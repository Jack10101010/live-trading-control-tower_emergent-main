import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChartPanel } from '@/components/domain/ChartPanel';
import { type Candle } from '@/lib/chartData';
import {
  CHART_LAYERS,
  DEFAULT_LAYER_IDS,
  buildAnnotations,
  type LayerContext,
} from '@/lib/chartLayers';
import { useTrades, useMarketCandles, useOrderBlocks, useFairValueGaps, useLiquidityPools, useMarketStructure } from '@/hooks/useRepository';
import { api } from '@/lib/api';
import { cn } from '@/lib/utils';
import { ChartDataStatusStrip } from '@/components/domain/ChartDataStatusStrip';
import { deriveChartContext, resolveChartMode, type ChartMode } from '@/lib/chartContext';

/**
 * ChartWorkspace — the canonical chart surface of the Control Tower.
 *
 * Phase 24: candles come from the Market Data Service through the SAME pipeline
 * (useMarketCandles → /market-data/candles → engine → active provider). This
 * component additionally owns: the timeframe switcher (M1…D1), range-based
 * historical back-scroll (older bars are RANGE queries merged in front of the
 * base window), live polling (in `useMarketCandles`), and the M1 inspector
 * (click a parent bar → its underlying M1 candles). Replay is unchanged: fixed
 * M15, replay provider, cursor-driven — no polling, no back-scroll.
 */

const TIMEFRAMES = ['M1', 'M5', 'M15', 'H1', 'H4', 'D1'] as const;
const TF_S: Record<string, number> = { M1: 60, M5: 300, M15: 900, H1: 3600, H4: 14400, D1: 86400 };
const SCROLL_CHUNK = 300; // bars per historical range request

const isoOf = (unix: number) => new Date(unix * 1000).toISOString();

/** Merge older (prepended) bars in front of the base window, deduped by time. */
function mergeBars(older: Candle[], base: Candle[]): Candle[] {
  if (!older.length) return base;
  if (!base.length) return older;
  const cut = base[0].time;
  const head = older.filter((b) => b.time < cut);
  return head.length ? [...head, ...base] : base;
}

export interface ChartWorkspaceProps {
  instrument: string;
  mode: 'live' | 'replay';
  /** Replay virtual time (unix seconds). Omit for live. */
  cursor?: number;
  /** Candle window end (replay session end). Omit for the live edge. */
  endISO?: string;
  count?: number;
  height?: number;
  className?: string;
  volume?: boolean;
  /** Show the layer-toggle / timeframe bar (default true). */
  controls?: boolean;
  /** TradingView-style vertical resize: the workspace owns its height and renders a
   *  drag divider at the bottom (persisted). Replay stays non-resizable (fills its
   *  own layout). */
  resizable?: boolean;
  initialHeight?: number;
  /** Explicit ChartMode for the status system (e.g. 'historical-explorer'). Defaults
   *  to a mapping of `mode` ('live'→'live-trading', 'replay'→'replay'). */
  chartMode?: ChartMode;
}

const HEIGHT_KEY = 'ct.chartHeight';
const clampHeight = (h: number) => Math.min(900, Math.max(220, h));

export function ChartWorkspace({
  instrument,
  mode,
  cursor,
  endISO,
  count = 220,
  height = 0,
  className,
  volume = true,
  controls = true,
  resizable = false,
  initialHeight = 420,
  chartMode,
}: ChartWorkspaceProps) {
  // Resizable height (live dashboards): owned here, persisted across sessions.
  const [chartHeight, setChartHeight] = useState<number>(() => {
    const saved = Number(typeof localStorage !== 'undefined' ? localStorage.getItem(HEIGHT_KEY) : NaN);
    return clampHeight(Number.isFinite(saved) && saved > 0 ? saved : initialHeight);
  });
  const heightRef = useRef(chartHeight);
  heightRef.current = chartHeight;
  const onResizeHandleDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = heightRef.current;
    let latest = startH;
    const move = (ev: PointerEvent) => {
      latest = clampHeight(startH + (ev.clientY - startY));
      setChartHeight(latest);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      try { localStorage.setItem(HEIGHT_KEY, String(latest)); } catch { /* ignore */ }
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }, []);
  // Timeframe — operator-selectable on live charts; replay stays on M15 (its
  // recorded scenario timeframe).
  const [timeframe, setTimeframe] = useState<string>('M15');
  const effectiveTf = mode === 'replay' ? 'M15' : timeframe;

  // Base window from the Market Data Service (live: polled; replay: immutable).
  const feed = useMarketCandles(instrument, {
    provider: mode === 'replay' ? 'replay' : undefined,
    count,
    endISO,
    timeframe: effectiveTf,
    live: mode === 'live',
  });

  // Historical back-scroll (live only): older bars fetched as RANGE queries and
  // merged in front of the base window. Reset when the series identity changes.
  // `seriesKey` guards against two bug classes found in the Phase-24 debug pass:
  // (a) a late range response landing after a timeframe switch must be IGNORED
  //     (it belongs to the old series), and
  // (b) each prepend re-fires the left-edge callback while still zoomed out, so
  //     chunk loads are cooled down and capped — without this a single zoom-out
  //     cascaded into dozens of requests, blowing the provider rate budget.
  const seriesKey = `${instrument}|${effectiveTf}|${mode}`;
  const seriesKeyRef = useRef(seriesKey);
  seriesKeyRef.current = seriesKey;
  const [olderBars, setOlderBars] = useState<Candle[]>([]);
  const loadingOlder = useRef(false);
  const exhausted = useRef(false);
  const lastChunkAt = useRef(0);
  const MAX_OLDER_BARS = 3000;
  useEffect(() => {
    setOlderBars([]);
    exhausted.current = false;
    loadingOlder.current = false;
    lastChunkAt.current = 0;
  }, [seriesKey]);

  const baseCandles = feed?.candles ?? [];
  const candles: Candle[] = useMemo(
    () => mergeBars(olderBars, baseCandles),
    [olderBars, baseCandles]
  );

  const handleNearLeftEdge = useCallback(() => {
    if (mode !== 'live' || loadingOlder.current || exhausted.current) return;
    if (Date.now() - lastChunkAt.current < 1_000) return; // cooldown: no cascades
    if (olderBars.length >= MAX_OLDER_BARS) return; // hard cap per series
    const first = (olderBars.length ? olderBars : baseCandles)[0];
    if (!first) return;
    loadingOlder.current = true;
    lastChunkAt.current = Date.now();
    const requestKey = seriesKeyRef.current;
    const step = TF_S[effectiveTf] ?? 900;
    api
      .marketCandles({
        symbol: instrument,
        timeframe: effectiveTf,
        start: isoOf(first.time - step * SCROLL_CHUNK),
        end: isoOf(first.time - 1),
        count: SCROLL_CHUNK,
      })
      .then((r) => {
        if (requestKey !== seriesKeyRef.current) {
          console.debug('[chart] back-scroll response IGNORED (stale series)', { requestKey, now: seriesKeyRef.current });
          return; // timeframe/instrument changed while in flight — drop it
        }
        console.debug('[chart] back-scroll response accepted', {
          requestId: (r as { requestId?: string }).requestId, tf: effectiveTf, count: r.candles.length,
          first: r.candles[0]?.time, last: r.candles[r.candles.length - 1]?.time,
        });
        if (!r.candles.length) {
          exhausted.current = true; // beginning of stored history
        } else {
          setOlderBars((prev) => mergeBars(r.candles, prev));
        }
      })
      .catch(() => undefined)
      .finally(() => {
        loadingOlder.current = false;
      });
  }, [mode, instrument, effectiveTf, olderBars, baseCandles]);

  // M1 inspector (Phase 24): click a parent bar → its underlying M1 candles.
  const [inspect, setInspect] = useState<{ time: number; bars: Candle[] } | null>(null);
  const handleBarClick = useCallback(
    (time: number) => {
      if (mode !== 'live' || effectiveTf === 'M1') return;
      const step = TF_S[effectiveTf] ?? 900;
      api
        .m1Window(instrument, isoOf(time), isoOf(time + step - 1))
        .then((r) => setInspect(r.candles.length ? { time, bars: r.candles } : null))
        .catch(() => setInspect(null));
    },
    [mode, instrument, effectiveTf]
  );
  useEffect(() => setInspect(null), [instrument, effectiveTf, mode]);

  // Shared data from the existing hooks (React Query dedupes — no duplicate fetch/state).
  const { live, ghost } = useTrades({ pair: instrument });
  const orderBlocks = useOrderBlocks(instrument, candles);
  const fairValueGaps = useFairValueGaps(instrument, candles);
  const liquidityPools = useLiquidityPools(instrument, candles);
  const marketStructure = useMarketStructure(instrument, candles);

  // Layer visibility — local workspace state seeded from the registry defaults.
  const [enabled, setEnabled] = useState<Set<string>>(() => new Set(DEFAULT_LAYER_IDS));
  const toggle = (id: string) =>
    setEnabled((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Stable per data-set: a per-render `new Date()` regenerated the layer context
  // (and every annotation array) on every render cycle. Live mode anchors "now"
  // to the last bar of the current feed instead of the wall clock.
  const lastBarTime = baseCandles.length ? baseCandles[baseCandles.length - 1].time : 0;
  const nowISO = useMemo(
    () => (cursor != null ? new Date(cursor * 1000).toISOString()
      : endISO ?? (lastBarTime ? new Date((lastBarTime + 3600) * 1000).toISOString() : new Date().toISOString())),
    [cursor, endISO, lastBarTime]
  );

  const ctx: LayerContext = useMemo(
    () => ({ instrument, candles, nowISO, cursor, mode, trades: live, ghosts: ghost, snapshot: null, orderBlocks, fairValueGaps, liquidityPools, marketStructure }),
    [instrument, candles, nowISO, cursor, mode, live, ghost, orderBlocks, fairValueGaps, liquidityPools, marketStructure]
  );

  const { markers, zones, priceLines } = useMemo(() => buildAnnotations(enabled, ctx), [enabled, ctx]);

  // The single authoritative ChartContext (Chart Status System). Every status
  // surface — strip, future inspector, screenshot stamp, audit export — renders a
  // projection of THIS. Interpretation lives entirely in deriveChartContext.
  // Date.now() is snapshotted at compute time (deps change ~each poll), not read
  // every render, so it can't churn other memos.
  const chartContext = useMemo(
    () => deriveChartContext({
      mode: resolveChartMode(mode, chartMode),
      instrument,
      timeframe: effectiveTf,
      feed,
      candles,
      requestedCount: count,
      isLive: mode === 'live',
      nowMs: Date.now(),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, chartMode, instrument, effectiveTf, feed, candles, count]
  );

  return (
    <div
      className={cn('flex flex-col min-h-0', resizable ? '' : 'h-full', className)}
      style={resizable ? { height: chartHeight } : undefined}
    >
      {controls && (
        <div
          className="flex items-center gap-1.5 px-3 h-8 shrink-0 border-b overflow-x-auto"
          style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
          data-testid="chart-layer-bar"
        >
          <span className="text-2xs text-text-muted uppercase tracking-wide mr-1 shrink-0">Layers</span>
          {CHART_LAYERS.map((l) => {
            const on = enabled.has(l.id);
            return (
              <button
                key={l.id}
                onClick={() => toggle(l.id)}
                data-testid={`layer-toggle-${l.id}`}
                aria-pressed={on}
                className="h-5 px-2 text-2xs rounded-sm shrink-0 transition-colors"
                style={{
                  color: on ? 'var(--text)' : 'var(--text-muted)',
                  background: on ? 'var(--panel-2)' : 'transparent',
                  border: `1px solid ${on ? 'var(--border)' : 'transparent'}`,
                }}
              >
                {l.label}
              </button>
            );
          })}
          {mode === 'live' && (
            <>
              <span className="ml-auto text-2xs text-text-muted uppercase tracking-wide mr-1 shrink-0">TF</span>
              {TIMEFRAMES.map((tf) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  data-testid={`tf-${tf}`}
                  aria-pressed={timeframe === tf}
                  className="h-5 px-1.5 text-2xs mono rounded-sm shrink-0 transition-colors"
                  style={{
                    color: timeframe === tf ? 'var(--text)' : 'var(--text-muted)',
                    background: timeframe === tf ? 'var(--panel-2)' : 'transparent',
                    border: `1px solid ${timeframe === tf ? 'var(--border)' : 'transparent'}`,
                  }}
                >
                  {tf}
                </button>
              ))}
            </>
          )}
        </div>
      )}

      <div className="flex-1 min-h-0 relative">
        <div className="absolute inset-0">
          {candles.length > 0 ? (
            <ChartPanel
              instrument={instrument}
              timeframe={effectiveTf}
              candles={candles}
              height={height}
              className="h-full w-full"
              volume={volume}
              sessionShading={false}
              markers={markers}
              zones={zones}
              priceLines={priceLines}
              cursorTime={cursor}
              onNearLeftEdge={handleNearLeftEdge}
              onBarClick={handleBarClick}
            />
          ) : (
            <div
              className="h-full w-full flex flex-col items-center justify-center gap-2.5"
              data-testid="chart-feed-loading"
            >
              {feed === undefined ? (
                <>
                  <div
                    className="w-5 h-5 rounded-full animate-spin"
                    style={{
                      border: '2px solid color-mix(in srgb, var(--primary) 22%, transparent)',
                      borderTopColor: 'var(--primary)',
                    }}
                  />
                  <span className="text-2xs text-text-muted mono tracking-wide">Loading market feed…</span>
                </>
              ) : (
                <>
                  <div className="flex items-end gap-[3px] h-5 opacity-40" aria-hidden>
                    {[9, 15, 7, 12, 5].map((h, i) => (
                      <span key={i} className="w-[3px] rounded-sm" style={{ height: h, background: 'var(--text-muted)' }} />
                    ))}
                  </div>
                  <span className="text-xs text-text-2 mono">No candles from feed</span>
                  <span className="text-2xs text-text-muted">the active market-data provider returned no bars</span>
                </>
              )}
            </div>
          )}
        </div>

        {/* M1 inspector (Phase 24) — the clicked bar's underlying M1 candles. */}
        {inspect && (
          <div
            className="absolute top-2 right-2 z-30 rounded-md border shadow-lg"
            style={{ width: 380, background: 'var(--panel)', borderColor: 'var(--border)' }}
            data-testid="m1-inspector"
          >
            <div
              className="flex items-center gap-2 px-2.5 h-7 border-b"
              style={{ borderColor: 'var(--border-subtle)' }}
            >
              <span className="text-2xs mono text-text">
                M1 · {new Date(inspect.time * 1000).toISOString().slice(0, 16).replace('T', ' ')} ·{' '}
                {effectiveTf} bar · {inspect.bars.length} candles
              </span>
              <button
                className="ml-auto text-text-muted hover:text-text text-xs leading-none"
                onClick={() => setInspect(null)}
                aria-label="Close M1 inspector"
                data-testid="m1-inspector-close"
              >
                ✕
              </button>
            </div>
            <div style={{ height: 200 }}>
              <ChartPanel
                instrument={`${instrument}-M1`}
                timeframe="M1"
                candles={inspect.bars}
                height={0}
                className="h-full w-full"
                volume={false}
              />
            </div>
          </div>
        )}
      </div>

      {/* Chart Status System — a projection of the single ChartContext model. Lives
          here (never in ChartPanel), so every chart mounted through ChartWorkspace
          (Pair Dashboard, Market Data, Replay) self-describes automatically. */}
      <ChartDataStatusStrip context={chartContext} />

      {/* Drag divider (resizable mode) — TradingView-style vertical resize. */}
      {resizable && (
        <div
          data-testid="chart-resize-handle"
          onPointerDown={onResizeHandleDown}
          className="h-1.5 shrink-0 cursor-row-resize transition-colors"
          style={{ background: 'var(--border-subtle)' }}
          onMouseEnter={(e) => ((e.currentTarget as HTMLDivElement).style.background = 'var(--primary)')}
          onMouseLeave={(e) => ((e.currentTarget as HTMLDivElement).style.background = 'var(--border-subtle)')}
          title="Drag to resize chart"
        />
      )}
    </div>
  );
}
