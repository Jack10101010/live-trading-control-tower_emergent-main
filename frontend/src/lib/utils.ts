import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function sessionLabel(s: string): string {
  if (s === 'NewYork') return 'New York';
  if (s === 'NY_PM') return 'NY PM';
  return s;
}

export function marketStateLabel(s: string): string {
  return s.replace(/([A-Z])/g, ' $1').trim().replace(/^Bull/, 'Bull /').replace(/^Bear/, 'Bear /');
}

export function marketStateGlyph(s: string): string {
  switch (s) {
    case 'BullExpand':
      return '▲▲';
    case 'BullCompress':
      return '▲◇';
    case 'BullChop':
      return '▲~';
    case 'BearExpand':
      return '▼▼';
    case 'BearCompress':
      return '▼◇';
    case 'BearChop':
      return '▼~';
    default:
      return '◇';
  }
}

export function marketStateVar(s: string): string {
  switch (s) {
    case 'BullExpand':
      return 'var(--ms-bull-expand)';
    case 'BullCompress':
      return 'var(--ms-bull-compress)';
    case 'BullChop':
      return 'var(--ms-bull-chop)';
    case 'BearExpand':
      return 'var(--ms-bear-expand)';
    case 'BearCompress':
      return 'var(--ms-bear-compress)';
    case 'BearChop':
      return 'var(--ms-bear-chop)';
    default:
      return 'var(--neutral)';
  }
}

export function laneLabel(lane: string): string {
  return lane.charAt(0).toUpperCase() + lane.slice(1).replace('_', ' ');
}

export function eligibilityLabel(action: string): string {
  switch (action) {
    case 'LABEL':
      return 'Always Allow';
    case 'STATE_ONLY':
      return 'Block Chop';
    case 'DIRECTION_AWARE':
      return 'Follow Trend';
    case 'DISABLE':
      return 'Never Trade';
    default:
      return action;
  }
}

export function eligibilityColor(action: string): string {
  switch (action) {
    case 'LABEL':
      return 'var(--elig-always-allow)';
    case 'STATE_ONLY':
      return 'var(--elig-block-chop)';
    case 'DIRECTION_AWARE':
      return 'var(--elig-follow-trend)';
    case 'DISABLE':
      return 'var(--elig-never-trade)';
    default:
      return 'var(--text-muted)';
  }
}

export function badgeColor(badge: string): string {
  switch (badge) {
    case 'NATIVE':
      return 'var(--valid-native)';
    case 'RESCORE':
      return 'var(--valid-rescore)';
    case 'BASE':
      return 'var(--valid-base)';
    case 'INSUFFICIENT':
      return 'var(--valid-insufficient)';
    case 'NOT_TESTED':
      return 'var(--valid-nottested)';
    default:
      return 'var(--text-muted)';
  }
}

export function laneColor(lane: string): string {
  switch (lane) {
    case 'live':
      return 'var(--live)';
    case 'demo':
      return 'var(--mode-demo)';
    case 'ghost':
      return 'var(--ghost)';
    case 'experimental':
      return 'var(--warning)';
    case 'research_forward':
      return 'var(--recommendation)';
    default:
      return 'var(--paused)';
  }
}

/** Scenario key parser: "EURUSD:london:BOS:long:BullExpand" → parts */
export function parseScenarioKey(key: string) {
  const [instrument, session, structure, direction, marketState] = key.split(':');
  return { instrument, session, structure, direction, marketState };
}

/** Build cell key from parts */
export function buildCellKey(session: string, structure: string, direction: string, marketState: string): string {
  const s = session.charAt(0).toLowerCase() + session.slice(1);
  const d = direction.toLowerCase();
  return `${s}:${structure}:${d}:${marketState}`;
}
