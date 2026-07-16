/**
 * Matrix expansion — deterministic synthesis of the full 24 × 6 = 144 cell
 * grid from the (typically sparse) representative cells the backend / fixture
 * provides. Same input → same output, so the grid is stable across renders
 * and reloads. When the backend later returns fully expanded matrices, this
 * util becomes a no-op passthrough (only fills missing cells).
 */

import type {
  PolicyCell,
  PolicyMatrixData,
  Session,
  Structure,
  Direction,
  MarketState,
  ValidationBadge,
  EligibilityAction,
} from '@/types/domain';

const SESSIONS: Session[] = ['London', 'Lull', 'NewYork', 'NY_PM', 'Asia', 'Outside'];
const STRUCTURES: Structure[] = ['BOS', 'CHoCH'];
const DIRECTIONS: Direction[] = ['Long', 'Short'];
const MARKET_STATES: MarketState[] = [
  'BullExpand',
  'BullCompress',
  'BullChop',
  'BearExpand',
  'BearCompress',
  'BearChop',
];

function hash(str: string): number {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 0xffffffff;
}

function synthCell(
  instrument: string,
  session: Session,
  structure: Structure,
  direction: Direction,
  marketState: MarketState,
  cohortBaseTargets: Record<string, number>,
  overrides: Map<string, PolicyCell>
): PolicyCell {
  const sLower = session.charAt(0).toLowerCase() + session.slice(1);
  const dLower = direction.toLowerCase();
  const key = `${sLower}:${structure}:${dLower}:${marketState}`;
  const override = overrides.get(key);
  if (override) return override;

  const seed = hash(`${instrument}:${key}`);
  const cohortBaseKey = `${sLower}:${structure}:${dLower}`;
  const baseTarget = cohortBaseTargets[cohortBaseKey] ?? 2.0;

  const isChopState = marketState.endsWith('Chop');
  const isBullState = marketState.startsWith('Bull');
  const followsTrend =
    (isBullState && direction === 'Long') || (!isBullState && direction === 'Short');

  const eligibility: {
    action: EligibilityAction;
    mode: 'Custom' | 'Inherit' | 'Block' | 'Research';
    resolvedAllowed: boolean;
  } = (() => {
    if (isChopState && seed < 0.6) {
      return { action: 'STATE_ONLY', mode: 'Custom', resolvedAllowed: false };
    }
    if (!followsTrend && seed < 0.55) {
      return { action: 'DIRECTION_AWARE', mode: 'Custom', resolvedAllowed: false };
    }
    if (seed < 0.05) {
      return { action: 'DISABLE', mode: 'Block', resolvedAllowed: false };
    }
    if (followsTrend) {
      return { action: 'DIRECTION_AWARE', mode: 'Custom', resolvedAllowed: true };
    }
    return { action: 'LABEL', mode: 'Custom', resolvedAllowed: true };
  })();

  const rrLadder = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0];
  const targetIdx = Math.min(
    Math.max(rrLadder.indexOf(baseTarget) + Math.floor((seed - 0.5) * 6), 4),
    rrLadder.length - 1
  );
  const rr = eligibility.resolvedAllowed ? rrLadder[targetIdx] : baseTarget;
  const risk =
    eligibility.action === 'DISABLE'
      ? 0
      : eligibility.resolvedAllowed
      ? Math.round((0.5 + seed * 0.75) * 100) / 100
      : 0.5;

  const badges: ValidationBadge[] = ['NATIVE', 'NATIVE', 'RESCORE', 'BASE', 'INSUFFICIENT', 'NOT_TESTED'];
  const badge = badges[Math.floor(seed * badges.length)];
  const sampleSize = badge === 'NOT_TESTED' ? 0 : Math.floor(15 + seed * 70);
  const expectancy = eligibility.resolvedAllowed
    ? Math.round((seed * 0.6 - 0.05) * 100) / 100
    : Math.round(seed * -0.3 * 100) / 100;
  const winRate = 0.24 + seed * 0.24;
  const profitFactor = 0.8 + seed * 1.2;
  const confidence =
    badge === 'NATIVE' ? 0.85 + seed * 0.14 : badge === 'RESCORE' ? 0.6 + seed * 0.2 : 0.4 + seed * 0.3;

  const recStatuses: PolicyCell['recommendationStatus'][] = [
    'none',
    'none',
    'none',
    'none',
    'validating',
    'new',
    'deployed',
    'accepted',
  ];
  const recStatus = recStatuses[Math.floor(seed * recStatuses.length)];

  return {
    policyCellKey: key,
    cohort: { session, structure, direction, instrument },
    marketState,
    eligibility,
    target: { rr, source: seed > 0.35 ? 'cell' : 'cohortBase' },
    risk: { pct: risk, source: seed > 0.4 ? 'cell' : 'cohortBase' },
    evidence: {
      badge,
      sampleSize,
      expectancyR: expectancy,
      winRate: Math.round(winRate * 100) / 100,
      profitFactor: Math.round(profitFactor * 100) / 100,
      confidence: Math.round(confidence * 100) / 100,
      inSampleCaveat: sampleSize < 25 ? 'thin sample' : '',
    },
    recommendationStatus: recStatus,
    provenanceRecommendationId: null,
  };
}

export function expandMatrix(instrument: string, sourceMatrix: PolicyMatrixData | undefined): PolicyMatrixData {
  const source = sourceMatrix ?? {
    instrument,
    cohortAxis: { sessions: SESSIONS, structures: STRUCTURES, directions: DIRECTIONS },
    cohortBaseTargets: {},
    cells: {},
  };

  const overrides = new Map<string, PolicyCell>();
  Object.entries(source.cells).forEach(([key, cell]) => {
    const [session, structure, direction] = key.split(':');
    const cohort = cell.cohort ?? {
      session: (session.charAt(0).toUpperCase() + session.slice(1)) as Session,
      structure: structure as Structure,
      direction: (direction.charAt(0).toUpperCase() + direction.slice(1)) as Direction,
      instrument,
    };
    overrides.set(key, { ...cell, cohort });
  });

  const baseTargets: Record<string, number> = { ...source.cohortBaseTargets };
  SESSIONS.forEach((session) => {
    STRUCTURES.forEach((structure) => {
      DIRECTIONS.forEach((direction) => {
        const s = session.charAt(0).toLowerCase() + session.slice(1);
        const d = direction.toLowerCase();
        const k = `${s}:${structure}:${d}`;
        if (!(k in baseTargets)) {
          const seed = hash(`${instrument}:cohort:${k}`);
          baseTargets[k] = [1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0][Math.floor(seed * 7)];
        }
      });
    });
  });

  const cells: Record<string, PolicyCell> = {};
  SESSIONS.forEach((session) => {
    STRUCTURES.forEach((structure) => {
      DIRECTIONS.forEach((direction) => {
        MARKET_STATES.forEach((marketState) => {
          const s = session.charAt(0).toLowerCase() + session.slice(1);
          const d = direction.toLowerCase();
          const key = `${s}:${structure}:${d}:${marketState}`;
          cells[key] = synthCell(instrument, session, structure, direction, marketState, baseTargets, overrides);
        });
      });
    });
  });

  return {
    instrument,
    cohortAxis: { sessions: SESSIONS, structures: STRUCTURES, directions: DIRECTIONS },
    cohortBaseTargets: baseTargets,
    cells,
  };
}
// NOTE (Phase 16): `synthCandles` was removed — chart OHLC now comes exclusively from
// the Market Data Engine (`/market-data/candles`) via ChartWorkspace. One candle pipeline.
