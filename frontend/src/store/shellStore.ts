import { create } from 'zustand';
import type { MarketState } from '@/types/domain';

export type MatrixLens = 'eligibility' | 'targets' | 'risk' | 'recommendations' | 'validation';

export type ThemeName = 'console-dark' | 'midnight-navy' | 'premium-light';

export type InspectorPayload =
  | { kind: 'policyCell'; instrument: string; cellKey: string }
  | { kind: 'trade'; tradeId: string }
  | { kind: 'ghost'; ghostTradeId: string }
  | { kind: 'blocked'; blockedIntentId: string }
  | { kind: 'deployment'; deploymentId: string }
  | { kind: 'deploymentManifest'; deploymentId: string }
  | { kind: 'recommendation'; recommendationId: string }
  | { kind: 'decisionChain'; decisionId: string }
  | null;

/**
 * The single operator-preferences model (Phase 4.5). Everything that was
 * previously persisted inconsistently (theme unpersisted; inspector width and
 * replay speed in separate `localStorage` keys) is now one blob under
 * `ct.operatorPrefs`, mirrored to the runtime overlay (`PUT /operator/preferences`)
 * by `usePreferenceSync`. View state, not trading truth — no audit event.
 */
export interface OperatorPreferences {
  theme: ThemeName;
  inspectorWidth: number;
  eventDockOpen: boolean;
  matrixLens: MatrixLens;
  replaySpeed: number;
}

interface ShellState {
  // Scope
  scope: 'fleet' | 'pair' | 'broker' | 'account';
  activePair: string;
  setActivePair: (pair: string) => void;

  // Theme
  theme: ThemeName;
  setTheme: (t: ThemeName) => void;

  // Inspector
  inspector: InspectorPayload;
  openInspector: (p: InspectorPayload) => void;
  closeInspector: () => void;
  inspectorWidth: number;
  setInspectorWidth: (w: number) => void;

  // Command palette
  paletteOpen: boolean;
  setPaletteOpen: (o: boolean) => void;

  // Matrix lens
  matrixLens: MatrixLens;
  setMatrixLens: (l: MatrixLens) => void;

  // Matrix state filter (for lens views that show one market state at a time)
  matrixStateFilter: MarketState | 'all';
  setMatrixStateFilter: (s: MarketState | 'all') => void;

  // Event dock
  eventDockOpen: boolean;
  setEventDockOpen: (o: boolean) => void;

  // Replay runtime — cursor/pair/playing are ephemeral local runtime (in
  // `ct.replay`); `speed` is an operator preference (in the prefs model).
  replay: ReplayRuntime;
  ensureReplayPair: (pair: string, start: number, end: number, deepLink?: number) => void;
  setReplayCursor: (cursor: number) => void;
  setReplaySpeed: (speed: number) => void;
  setReplayPlaying: (playing: boolean) => void;

  // Operator preferences (consolidated)
  preferences: () => OperatorPreferences;
  hydratePreferences: (p: Record<string, unknown>) => void;

  // Realtime read-side connection status (Phase 5)
  realtime: RealtimeStatus;
  setRealtime: (patch: Partial<RealtimeStatus>) => void;

  // Global mode banner (execution posture)
  operationMode: 'mock' | 'demo' | 'live';
}

export type RealtimeConnState = 'connecting' | 'live' | 'reconnecting' | 'offline';

export interface RealtimeStatus {
  state: RealtimeConnState;
  lastSeq: number;
  lastEventAt: number | null;
}

export interface ReplayRuntime {
  pair: string | null;
  cursor: number;
  speed: number;
  playing: boolean;
}

// ---------------------------------------------------------------------------
// Preference persistence — one consolidated `localStorage` blob, with a
// one-time migration from the legacy per-key storage.
// ---------------------------------------------------------------------------

const PREFS_KEY = 'ct.operatorPrefs';
const REPLAY_KEY = 'ct.replay';

const DEFAULT_PREFS: OperatorPreferences = {
  theme: 'console-dark',
  inspectorWidth: 440,
  eventDockOpen: true,
  matrixLens: 'eligibility',
  replaySpeed: 4,
};

function readJSON<T>(key: string): Partial<T> {
  try {
    const raw = typeof localStorage !== 'undefined' && localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as Partial<T>) : {};
  } catch {
    return {};
  }
}

function loadPrefs(): OperatorPreferences {
  const stored = readJSON<OperatorPreferences>(PREFS_KEY);
  if (Object.keys(stored).length > 0) {
    return { ...DEFAULT_PREFS, ...stored };
  }
  // One-time migration from legacy keys (ct.inspectorWidth, ct.replay.speed).
  const legacyWidth =
    (typeof localStorage !== 'undefined' && Number(localStorage.getItem('ct.inspectorWidth'))) || undefined;
  const legacyReplaySpeed = Number(readJSON<ReplayRuntime>(REPLAY_KEY).speed) || undefined;
  return {
    ...DEFAULT_PREFS,
    ...(legacyWidth ? { inspectorWidth: legacyWidth } : {}),
    ...(legacyReplaySpeed ? { replaySpeed: legacyReplaySpeed } : {}),
  };
}

function persistPrefs(p: OperatorPreferences): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  } catch {
    /* view pref only */
  }
}

function loadReplay(speed: number): ReplayRuntime {
  const p = readJSON<ReplayRuntime>(REPLAY_KEY);
  return {
    pair: typeof p.pair === 'string' ? p.pair : null,
    cursor: Number(p.cursor) || 0,
    speed,
    playing: false, // never auto-resume playback on load
  };
}

function persistReplay(r: ReplayRuntime): void {
  try {
    // Cursor/pair only — speed is persisted with preferences.
    localStorage.setItem(REPLAY_KEY, JSON.stringify({ pair: r.pair, cursor: r.cursor }));
  } catch {
    /* view pref only */
  }
}

const INITIAL_PREFS = loadPrefs();
// Apply persisted theme immediately so a refresh keeps it (no flash to default).
if (typeof document !== 'undefined') {
  document.documentElement.setAttribute('data-theme', INITIAL_PREFS.theme);
}

export const useShellStore = create<ShellState>((set, get) => {
  const snapshot = (): OperatorPreferences => {
    const s = get();
    return {
      theme: s.theme,
      inspectorWidth: s.inspectorWidth,
      eventDockOpen: s.eventDockOpen,
      matrixLens: s.matrixLens,
      replaySpeed: s.replay.speed,
    };
  };
  const savePrefs = () => persistPrefs(snapshot());

  return {
    scope: 'fleet',
    activePair: 'EURUSD',
    setActivePair: (pair) => set({ activePair: pair, scope: 'pair' }),

    theme: INITIAL_PREFS.theme,
    setTheme: (theme) => {
      document.documentElement.setAttribute('data-theme', theme);
      set({ theme });
      savePrefs();
    },

    inspector: null,
    openInspector: (payload) => set({ inspector: payload }),
    closeInspector: () => set({ inspector: null }),

    inspectorWidth: INITIAL_PREFS.inspectorWidth,
    setInspectorWidth: (w) => {
      const clamped = Math.max(360, Math.min(760, Math.round(w)));
      set({ inspectorWidth: clamped });
      savePrefs();
    },

    paletteOpen: false,
    setPaletteOpen: (paletteOpen) => set({ paletteOpen }),

    matrixLens: INITIAL_PREFS.matrixLens,
    setMatrixLens: (matrixLens) => {
      set({ matrixLens });
      savePrefs();
    },

    matrixStateFilter: 'all',
    setMatrixStateFilter: (matrixStateFilter) => set({ matrixStateFilter }),

    eventDockOpen: INITIAL_PREFS.eventDockOpen,
    setEventDockOpen: (eventDockOpen) => {
      set({ eventDockOpen });
      savePrefs();
    },

    replay: loadReplay(INITIAL_PREFS.replaySpeed),
    ensureReplayPair: (pair, start, end, deepLink) =>
      set((s) => {
        const prev = s.replay;
        const inWindow = (t: number) => t >= start && t <= end;
        let cursor: number;
        if (deepLink !== undefined && inWindow(deepLink)) cursor = deepLink;
        else if (prev.pair === pair && inWindow(prev.cursor)) cursor = prev.cursor;
        else cursor = start;
        const next = { ...prev, pair, cursor, playing: false };
        persistReplay(next);
        return { replay: next };
      }),
    setReplayCursor: (cursor) =>
      set((s) => {
        const next = { ...s.replay, cursor };
        persistReplay(next);
        return { replay: next };
      }),
    setReplaySpeed: (speed) => {
      set((s) => ({ replay: { ...s.replay, speed } }));
      savePrefs(); // speed is a preference
    },
    setReplayPlaying: (playing) => set((s) => ({ replay: { ...s.replay, playing } })),

    realtime: { state: 'connecting', lastSeq: 0, lastEventAt: null },
    setRealtime: (patch) => set((s) => ({ realtime: { ...s.realtime, ...patch } })),

    preferences: snapshot,
    hydratePreferences: (p) =>
      set((s) => {
        const next: Partial<ShellState> = {};
        const themes: ThemeName[] = ['console-dark', 'midnight-navy', 'premium-light'];
        const lenses: MatrixLens[] = ['eligibility', 'targets', 'risk', 'recommendations', 'validation'];
        if (typeof p.theme === 'string' && themes.includes(p.theme as ThemeName) && p.theme !== s.theme) {
          document.documentElement.setAttribute('data-theme', p.theme);
          next.theme = p.theme as ThemeName;
        }
        if (typeof p.inspectorWidth === 'number') next.inspectorWidth = Math.max(360, Math.min(760, p.inspectorWidth));
        if (typeof p.eventDockOpen === 'boolean') next.eventDockOpen = p.eventDockOpen;
        if (typeof p.matrixLens === 'string' && lenses.includes(p.matrixLens as MatrixLens)) next.matrixLens = p.matrixLens as MatrixLens;
        if (typeof p.replaySpeed === 'number') next.replay = { ...s.replay, speed: p.replaySpeed };
        return next;
      }),

    operationMode: 'live',
  };
});
