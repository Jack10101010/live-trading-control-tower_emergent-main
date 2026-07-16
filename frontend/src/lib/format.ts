/**
 * Number-Format Canon — §E of the Master Generation Brief.
 * All formatters return strings. Never format ad hoc; always use these.
 */

export function fmtR(value: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (value == null || Number.isNaN(value)) return '—';
  const sign = opts.sign !== false && value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}R`;
}

export function fmtMoney(value: number | null | undefined, currency: string = 'USD'): string {
  if (value == null || Number.isNaN(value)) return '—';
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value);
  const symbol = currency === 'USD' ? '$' : currency + ' ';
  return `${sign}${symbol}${abs.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function fmtPercent(value: number | null | undefined, digits: number = 1): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${value.toFixed(digits)}%`;
}

export function fmtPct01(value: number | null | undefined, digits: number = 1): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function fmtConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function fmtSampleSize(n: number): string {
  return `n=${n}`;
}

export function fmtProbability(p: number): string {
  return `P=${p.toFixed(2)}`;
}

export function fmtPrice(value: number, digits: number = 5): string {
  return value.toFixed(digits);
}

export function fmtPips(value: number): string {
  return `${value.toFixed(1)} pips`;
}

export function fmtPackageVersion(version: number): string {
  return `Package v${version}`;
}

export function fmtHash(hash: string, length: number = 8): string {
  if (!hash) return '—';
  const clean = hash.startsWith('sha256:') ? hash.slice(7) : hash;
  return `${clean.slice(0, length)}…`;
}

export function fmtRelative(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const seconds = Math.round((now.getTime() - d.getTime()) / 1000);
  if (seconds < 0) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return d.toISOString().slice(0, 10);
}

export function fmtUtc(iso: string): string {
  return new Date(iso).toISOString().replace('.000Z', 'Z');
}

export function fmtProfitFactor(pf: number): string {
  return pf.toFixed(2);
}

export function fmtExpectancy(er: number): string {
  const sign = er > 0 ? '+' : '';
  return `${sign}${er.toFixed(2)}R`;
}
