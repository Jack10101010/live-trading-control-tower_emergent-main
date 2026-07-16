import { useEffect, useRef, useState } from 'react';
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type SeriesMarker,
} from 'lightweight-charts';
import { cn } from '@/lib/utils';
import {
  deriveVolume,
  sessionBands,
  type Candle,
  type ChartMarker,
  type ChartZone,
  type ChartPriceLine,
  type LinePoint,
} from '@/lib/chartData';

/**
 * ChartPanel — the single reusable Lightweight Charts renderer (Phase 22: pro-terminal
 * polish). READ-ONLY: no drawing tools, no click-to-trade, no order drag. Everything
 * comes through props; presentation only. Reused across Replay, Trades, Inspector,
 * Edge Monitor and Analytics.
 */

interface ChartPanelProps {
  /** Candle series (always supplied by ChartWorkspace from the Market Data Engine). */
  candles?: Candle[];
  instrument?: string;
  /** Timeframe label for the floating badge (default M15 — the engine default). */
  timeframe?: string;
  height?: number;
  className?: string;
  kind?: 'candles' | 'line';
  lineData?: LinePoint[];
  linePrecision?: number;
  volume?: boolean;
  sessionShading?: boolean;
  priceLines?: ChartPriceLine[];
  markers?: ChartMarker[];
  zones?: ChartZone[];
  /** Replay cursor time (unix seconds); draws a vertical line. */
  cursorTime?: number;
  /** Fired (throttled) when the user scrolls near the oldest loaded bar —
   *  ChartWorkspace uses it to range-load older history (Phase 24). */
  onNearLeftEdge?: () => void;
  /** Fired with the clicked bar's time — drives the M1 inspector (Phase 24). */
  onBarClick?: (time: number) => void;
}

/** Resolve `var(--token)` to a concrete colour for canvas drawing. */
function resolveColor(c: string): string {
  if (!c) return '#888';
  if (c.startsWith('var(')) {
    const name = c.slice(4, -1).trim();
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || '#888';
  }
  return c;
}

// Stable empties: default-parameter literals (`= []`) create a NEW identity every
// render; used as effect deps they re-run the effect per render — combined with an
// unconditional setState below, that was a silent infinite re-render loop that
// starved React Router's startTransition-based navigation (URL changed, view froze).
const EMPTY_CANDLES: Candle[] = [];
const EMPTY_LINE: LinePoint[] = [];

interface Legend {
  open: number;
  high: number;
  low: number;
  close: number;
  changePct: number;
  up: boolean;
}

function toLegend(o: number, h: number, l: number, c: number): Legend {
  const changePct = o ? ((c - o) / o) * 100 : 0;
  return { open: o, high: h, low: l, close: c, changePct, up: c >= o };
}

/** setState updater that keeps the previous object when values are equal —
 *  breaks any render→effect→setState cycle at the state-identity level. */
const sameLegend = (next: Legend) => (prev: Legend | null): Legend =>
  prev && prev.open === next.open && prev.high === next.high &&
  prev.low === next.low && prev.close === next.close ? prev : next;

export function ChartPanel({
  candles,
  instrument = 'EURUSD',
  timeframe = 'M15',
  height = 380,
  className,
  kind = 'candles',
  lineData = EMPTY_LINE,
  linePrecision = 2,
  volume = false,
  sessionShading = false,
  priceLines = [],
  markers = [],
  zones = [],
  cursorTime,
  onNearLeftEdge,
  onBarClick,
}: ChartPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainRef = useRef<ISeriesApi<'Candlestick'> | ISeriesApi<'Line'> | null>(null);
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const prevCandlesRef = useRef<Candle[]>([]);
  const candlesRef = useRef<Candle[]>([]);
  const onNearLeftEdgeRef = useRef(onNearLeftEdge);
  const onBarClickRef = useRef(onBarClick);
  onNearLeftEdgeRef.current = onNearLeftEdge;
  onBarClickRef.current = onBarClick;
  const priceLineHandles = useRef<Array<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>>>([]);
  const [redraw, setRedraw] = useState(0);
  const [boxes, setBoxes] = useState<
    Array<{ left: number; top: number; width: number; height: number; color: string; label?: string; band: boolean }>
  >([]);
  const [cursorX, setCursorX] = useState<number | null>(null);
  const [legend, setLegend] = useState<Legend | null>(null);

  const resolvedCandles: Candle[] = candles ?? EMPTY_CANDLES;
  const allZones: ChartZone[] = sessionShading ? [...sessionBands(resolvedCandles), ...zones] : zones;
  const precision = kind === 'line' ? linePrecision : 5;

  // Create chart + main series once per data identity.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const axisText = resolveColor('var(--text-muted)');
    const border = resolveColor('var(--border-subtle)');
    const grid = resolveColor('var(--grid)');
    const crosshair = resolveColor('var(--crosshair)');
    const labelBg = resolveColor('var(--panel-3)');

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: axisText,
        fontFamily: "'JetBrains Mono', ui-monospace, SFMono-Regular, monospace",
        fontSize: 11,
        attributionLogo: false, // drop the TradingView watermark — reads as a custom terminal
      },
      localization: {
        locale: 'en-US',
        priceFormatter: (p: number) => p.toFixed(precision),
      },
      grid: {
        vertLines: { color: grid, style: LineStyle.Dotted },
        horzLines: { color: grid, style: LineStyle.Dotted },
      },
      rightPriceScale: {
        borderColor: border,
        borderVisible: true,
        entireTextOnly: true,
        ticksVisible: false,
        scaleMargins: { top: 0.08, bottom: volume && kind === 'candles' ? 0.26 : 0.1 },
      },
      timeScale: {
        borderColor: border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6, // breathing room past the last bar
        barSpacing: 9,
        minBarSpacing: 2,
        ticksVisible: false,
        lockVisibleTimeRangeOnResize: true,
      },
      crosshair: {
        mode: kind === 'candles' ? CrosshairMode.Magnet : CrosshairMode.Normal,
        vertLine: { color: crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: labelBg },
        horzLine: { color: crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: labelBg },
      },
      autoSize: true,
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      // TradingView-style scaling: drag the price axis to scale vertically (this
      // switches the scale to manual), drag the time axis to stretch bars, wheel to
      // zoom, double-click either axis to reset back to auto-fit.
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: { time: true, price: true },
        axisDoubleClickReset: { time: true, price: true },
      },
      kineticScroll: { touch: true, mouse: false },
    });

    let main: ISeriesApi<'Candlestick'> | ISeriesApi<'Line'>;
    if (kind === 'line') {
      main = chart.addLineSeries({
        color: resolveColor('var(--primary)'),
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 3,
        priceFormat: { type: 'price', precision: linePrecision, minMove: Math.pow(10, -linePrecision) },
      });
    } else {
      const up = resolveColor('var(--positive)');
      const down = resolveColor('var(--negative)');
      const cs = chart.addCandlestickSeries({
        upColor: up,
        downColor: down,
        borderUpColor: up,
        borderDownColor: down,
        wickUpColor: up,
        wickDownColor: down,
        borderVisible: true,
        priceFormat: { type: 'price', precision: 5, minMove: 0.00001 },
        priceLineVisible: true, // dashed line + label at the last price (like a pro terminal)
        priceLineColor: resolveColor('var(--text-muted)'),
        priceLineWidth: 1,
        priceLineStyle: LineStyle.Dashed,
        lastValueVisible: true,
      });
      main = cs;
    }

    if (volume && kind === 'candles') {
      const vol = chart.addHistogramSeries({
        priceScaleId: 'volume',
        priceFormat: { type: 'volume' },
        lastValueVisible: false,
        priceLineVisible: false,
      });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
      volRef.current = vol;
    }

    chartRef.current = chart;
    mainRef.current = main;
    prevCandlesRef.current = [];

    // `disposed` gates every async chart callback: once cleanup runs (unmount, or
    // StrictMode's synthetic remount), a late crosshair/range/resize callback must
    // NOT touch the removed chart — lightweight-charts throws "Object is disposed",
    // which with no ErrorBoundary would tear down the tree (black screen).
    let disposed = false;

    // Floating OHLC legend — defaults to the last bar, tracks the crosshair on hover.
    const onMove = (param: Parameters<Parameters<IChartApi['subscribeCrosshairMove']>[0]>[0]) => {
      if (disposed) return;
      const bar = param.seriesData?.get(main) as { open?: number; high?: number; low?: number; close?: number } | undefined;
      const cur = candlesRef.current;
      if (bar && bar.open != null && bar.close != null) {
        setLegend(sameLegend(toLegend(bar.open, bar.high ?? bar.close, bar.low ?? bar.close, bar.close)));
      } else if (cur.length) {
        const last = cur[cur.length - 1];
        setLegend(sameLegend(toLegend(last.open, last.high, last.low, last.close)));
      }
    };
    if (kind === 'candles') chart.subscribeCrosshairMove(onMove);

    // Bar click → M1 inspector (Phase 24).
    const onClick = (param: Parameters<Parameters<IChartApi['subscribeClick']>[0]>[0]) => {
      if (disposed) return;
      if (param.time != null) onBarClickRef.current?.(param.time as number);
    };
    chart.subscribeClick(onClick);

    const bump = () => { if (!disposed) setRedraw((n) => n + 1); };
    // Visible-range changes drive overlay repositioning AND left-edge history loading.
    const onRange = () => {
      if (disposed) return;
      bump();
      const lr = chart.timeScale().getVisibleLogicalRange();
      if (lr && lr.from < 12) onNearLeftEdgeRef.current?.();
    };
    chart.timeScale().subscribeVisibleTimeRangeChange(onRange);
    const ro = new ResizeObserver(bump);
    ro.observe(el);
    bump();

    return () => {
      disposed = true;
      ro.disconnect();
      // Tear down EVERY subscription before remove() (the range handler was
      // previously left attached — an asymmetry that risked a disposed-chart call).
      if (kind === 'candles') chart.unsubscribeCrosshairMove(onMove);
      chart.unsubscribeClick(onClick);
      chart.timeScale().unsubscribeVisibleTimeRangeChange(onRange);
      priceLineHandles.current = [];
      chart.remove();
      chartRef.current = null;
      mainRef.current = null;
      volRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, kind, volume, linePrecision]);

  // Data application — update-in-place so live ticks and history prepends never
  // recreate the chart (Phase 24). Three cases: tail update (same first bar →
  // series.update per new/changed last bars), prepend (same last bar, new first →
  // setData + shift the visible logical range so the view doesn't jump), reset
  // (symbol/timeframe change → setData + fitContent).
  useEffect(() => {
    const s = mainRef.current;
    const chart = chartRef.current;
    if (!s || !chart) return;
    if (kind === 'line') {
      (s as ISeriesApi<'Line'>).setData(lineData.map((p) => ({ time: p.time as Time, value: p.value })));
      chart.timeScale().fitContent();
      return;
    }
    let next = resolvedCandles;
    // Defensive: Lightweight Charts hard-asserts ascending unique times in setData/
    // update — a violation crashes the chart (blank canvas until remount). Guard and
    // warn loudly so any future data-composition bug degrades visibly instead.
    const unordered = next.some((c, i) => i > 0 && c.time <= next[i - 1].time);
    if (unordered) {
      console.warn('[chart] candle array unsorted/duplicated — sanitizing (data-composition bug upstream)');
      const byTime = new Map<number, Candle>();
      for (const c of next) byTime.set(c.time, c);
      next = [...byTime.values()].sort((a, b) => a.time - b.time);
    }
    const prev = prevCandlesRef.current;
    candlesRef.current = next;
    const cs = s as ISeriesApi<'Candlestick'>;
    const toBar = (c: Candle) => ({ time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close });
    const volAll = () => volRef.current?.setData(
      deriveVolume(next).map((v) => ({ time: v.time as Time, value: v.value, color: v.color })));

    if (prev.length && next.length && next[0].time === prev[0].time && next.length >= prev.length) {
      // Tail update (live polling): update the last known bar + any new bars.
      const volSeries = deriveVolume(next);
      for (let i = Math.max(0, prev.length - 1); i < next.length; i++) {
        cs.update(toBar(next[i]));
        volRef.current?.update({ time: volSeries[i].time as Time, value: volSeries[i].value, color: volSeries[i].color });
      }
    } else if (prev.length && next.length && next[next.length - 1].time === prev[prev.length - 1].time && next[0].time < prev[0].time) {
      // Prepend (historical scrolling): keep the user's view stable.
      const added = next.length - prev.length;
      const range = chart.timeScale().getVisibleLogicalRange();
      cs.setData(next.map(toBar));
      volAll();
      if (range) chart.timeScale().setVisibleLogicalRange({ from: range.from + added, to: range.to + added });
    } else {
      // Reset (first load / symbol / timeframe change).
      cs.setData(next.map(toBar));
      volAll();
      chart.timeScale().fitContent();
    }
    prevCandlesRef.current = next;
    if (next.length) {
      const last = next[next.length - 1];
      setLegend(sameLegend(toLegend(last.open, last.high, last.low, last.close)));
    } else {
      setLegend(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedCandles, lineData, kind]);

  // Price lines
  useEffect(() => {
    const s = mainRef.current as ISeriesApi<'Candlestick'> | null;
    if (!s) return;
    priceLineHandles.current.forEach((h) => s.removePriceLine(h));
    priceLineHandles.current = priceLines.map((pl) =>
      s.createPriceLine({
        price: pl.price,
        color: resolveColor(pl.color),
        lineWidth: 1,
        lineStyle: pl.lineStyle === 'dashed' ? LineStyle.Dashed : LineStyle.Solid,
        axisLabelVisible: true,
        title: pl.label,
      })
    );
  }, [priceLines, redraw]);

  // Markers
  useEffect(() => {
    const s = mainRef.current;
    if (!s) return;
    const sm: SeriesMarker<Time>[] = markers
      .map((m) => ({
        time: m.time as Time,
        position: m.position,
        color: resolveColor(m.color),
        shape: m.shape,
        text: m.text,
      }))
      // Lightweight Charts requires markers in ascending time order; callers may
      // pass them in entity order (e.g. by trade), so sort defensively here.
      .sort((a, b) => (a.time as number) - (b.time as number));
    s.setMarkers(sm);
  }, [markers]);

  // Overlay layer: zones + replay cursor, positioned from chart coordinates.
  useEffect(() => {
    const chart = chartRef.current;
    const s = mainRef.current as ISeriesApi<'Candlestick'> | null;
    const el = containerRef.current;
    if (!chart || !s || !el) return;
    const ts = chart.timeScale();
    const H = el.clientHeight;

    const nextBoxes = allZones
      .map((z) => {
        const x0 = ts.timeToCoordinate(z.time0 as Time);
        const x1 = ts.timeToCoordinate(z.time1 as Time);
        if (x0 == null || x1 == null) return null;
        let top = 0;
        let h = H;
        const band = !!z.fullHeight;
        if (!z.fullHeight && z.price0 != null && z.price1 != null) {
          const y0 = s.priceToCoordinate(z.price0);
          const y1 = s.priceToCoordinate(z.price1);
          if (y0 == null || y1 == null) return null;
          top = Math.min(y0, y1);
          h = Math.abs(y1 - y0);
        }
        const left = Math.min(x0, x1);
        const width = Math.max(2, Math.abs(x1 - x0));
        return { left, top, width, height: h, color: z.color, label: z.label, band };
      })
      .filter((b): b is NonNullable<typeof b> => b !== null);
    setBoxes(nextBoxes);

    if (cursorTime != null) {
      setCursorX(ts.timeToCoordinate(cursorTime as Time));
    } else {
      setCursorX(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [redraw, cursorTime, allZones.length]);

  const fmt = (n: number) => n.toFixed(precision);

  return (
    <div
      ref={containerRef}
      className={cn('relative w-full', className)}
      style={height > 0 ? { height } : undefined}
      data-testid={`chart-panel-${instrument}`}
    >
      {/* Overlay layer — pointer-events none, purely visual (read-only) */}
      <div className="absolute inset-0 pointer-events-none z-10 overflow-hidden">
        {boxes.map((b, i) => (
          <div
            key={i}
            className="absolute"
            style={{
              left: b.left,
              top: b.top,
              width: b.width,
              height: b.height,
              background: b.color,
              // Session bands stay flat; SMC zones get a hairline for a crisper edge.
              ...(b.band
                ? {}
                : { borderRadius: 1, boxShadow: 'inset 0 0 0 1px rgba(255,255,255,0.05)' }),
            }}
            title={b.label}
          />
        ))}
        {cursorX != null && (
          <div
            className="absolute top-0 bottom-0"
            style={{ left: cursorX, width: 1.5, background: 'var(--warning)', boxShadow: '0 0 6px var(--warning)' }}
          />
        )}
      </div>

      {/* Floating instrument · timeframe + OHLC legend (top-left), pro-terminal style */}
      {kind === 'candles' && (
        <div className="absolute top-2 left-2.5 z-20 pointer-events-none select-none flex flex-col gap-0.5">
          <div className="flex items-center gap-2 text-2xs mono">
            <span className="text-text font-medium tracking-wide">{instrument}</span>
            <span
              className="px-1 rounded-sm text-text-muted"
              style={{ background: 'color-mix(in srgb, var(--panel-3) 70%, transparent)' }}
            >
              {timeframe}
            </span>
          </div>
          {legend && (
            <div
              className="flex items-center gap-2 text-2xs mono px-1.5 py-0.5 rounded-sm"
              style={{
                background: 'color-mix(in srgb, var(--panel) 62%, transparent)',
                backdropFilter: 'blur(2px)',
              }}
            >
              <span className="text-text-muted">O<span className="text-text-2 ml-1">{fmt(legend.open)}</span></span>
              <span className="text-text-muted">H<span className="text-text-2 ml-1">{fmt(legend.high)}</span></span>
              <span className="text-text-muted">L<span className="text-text-2 ml-1">{fmt(legend.low)}</span></span>
              <span className="text-text-muted">
                C<span className="ml-1" style={{ color: legend.up ? 'var(--positive)' : 'var(--negative)' }}>{fmt(legend.close)}</span>
              </span>
              <span style={{ color: legend.up ? 'var(--positive)' : 'var(--negative)' }}>
                {legend.changePct >= 0 ? '+' : ''}
                {legend.changePct.toFixed(2)}%
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
