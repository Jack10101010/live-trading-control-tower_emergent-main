/**
 * Card-level provenance system (temporary migration aid) — behavioural and
 * structural guards.
 *
 * 1. The semantic→visual mapping is exhaustive and GREEN only for proven-real.
 * 2. Chart/candle provenance is dynamic per provider and defaults to non-real.
 * 3. Structural source guards (no-fabrication style): every <Panel> carries an
 *    explicit provenance, live-plane domain components are framed GREEN, and
 *    the known fixture screens stay RED — so a refactor cannot silently strip
 *    or flip the classification.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  PROVENANCE_TONE,
  provenanceTone,
  provenanceBadge,
  candleCardProvenance,
  type CardProvenance,
} from '../cardProvenance';

const SRC = path.resolve(__dirname, '../..');
const read = (rel: string) => readFileSync(path.join(SRC, rel), 'utf8');

describe('provenance → tone mapping', () => {
  it('maps every real class to green and every non-real class to red', () => {
    const green: CardProvenance[] = ['live', 'runtime-config', 'derived-live'];
    const red: CardProvenance[] = ['fixture', 'synthetic', 'replay', 'placeholder', 'mixed', 'unknown'];
    for (const p of green) expect(PROVENANCE_TONE[p], p).toBe('green');
    for (const p of red) expect(PROVENANCE_TONE[p], p).toBe('red');
    // exhaustive: no class exists outside the two lists
    expect(Object.keys(PROVENANCE_TONE).sort()).toEqual([...green, ...red].sort());
  });

  it("chrome ('none') renders no tone and no badge", () => {
    expect(provenanceTone('none')).toBe('none');
    expect(provenanceBadge('none')).toBeNull();
  });

  it('badges say LIVE / NON-LIVE, never anything softer', () => {
    expect(provenanceBadge('live')).toBe('LIVE');
    expect(provenanceBadge('runtime-config')).toBe('LIVE');
    expect(provenanceBadge('fixture')).toBe('NON-LIVE');
    expect(provenanceBadge('mixed')).toBe('NON-LIVE');
    expect(provenanceBadge('unknown')).toBe('NON-LIVE');
  });
});

describe('dynamic candle-card provenance', () => {
  it('is green only for genuine market-data sources', () => {
    expect(provenanceTone(candleCardProvenance('mt5'))).toBe('green');
    expect(provenanceTone(candleCardProvenance('polygon'))).toBe('green');
    expect(provenanceTone(candleCardProvenance('store'))).toBe('green');
  });
  it('is red for every Control-Tower-generated provider', () => {
    for (const p of ['fixture', 'mock_live', 'synthetic', 'replay', 'replay-snapshot']) {
      expect(provenanceTone(candleCardProvenance(p)), p).toBe('red');
    }
  });
  it('defaults to red for unknown/absent providers (never silently green)', () => {
    expect(provenanceTone(candleCardProvenance(undefined))).toBe('red');
    expect(provenanceTone(candleCardProvenance(null))).toBe('red');
    expect(provenanceTone(candleCardProvenance('surprise-provider'))).toBe('red');
  });
});

/* ── structural source guards ────────────────────────────────────────────── */

function allTsx(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'node_modules') continue;
      allTsx(full, acc);
    } else if (/\.tsx$/.test(entry) && !/\.test\.tsx$/.test(entry)) {
      acc.push(full);
    }
  }
  return acc;
}

describe('structural guards', () => {
  it('every <Panel usage carries an explicit provenance prop', () => {
    // Belt-and-braces beside the required TypeScript prop: catches spreads or
    // any future loosening of the prop type.
    const offenders: string[] = [];
    for (const file of allTsx(SRC)) {
      const text = readFileSync(file, 'utf8');
      const opens = [...text.matchAll(/<Panel[\s>]/g)];
      for (const m of opens) {
        const tag = text.slice(m.index!, text.indexOf('>', m.index!) + 1);
        if (!/provenance(=|\s*=)/.test(tag)) {
          offenders.push(`${path.relative(SRC, file)}: ${tag.slice(0, 80)}`);
        }
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('live-plane domain components are framed in SystemView with honest provenance', () => {
    const s = read('views/SystemView.tsx');
    // Adapter-fed pipelines (projection, runtime loop, ledger) must be DYNAMIC:
    // mock adapter ⇒ synthetic (RED), real adapter ⇒ live (GREEN). A static
    // "live" here would put mock balances inside a GREEN frame.
    for (const comp of ['OperationalDashboard', 'LiveRuntimePanel', 'TradeLedgerPanel']) {
      const mount = s.indexOf(`<${comp}`);
      expect(mount, `${comp} must be mounted`).toBeGreaterThan(-1);
      const before = s.slice(Math.max(0, mount - 250), mount);
      expect(before, `${comp} must use the adapter-dependent frame`)
        .toContain('<ProvenanceFrame provenance={adapterProvenance}');
    }
    // The adapter gate itself must be fail-closed: mock or unknown ⇒ synthetic.
    expect(s).toContain("rt?.broker?.kind && rt.broker.kind !== 'mock'");
    // Operator-owned durable stores and real config remain statically live.
    for (const comp of ['RecommendationPanel', 'SecurityBaselinePanel']) {
      const mount = s.indexOf(`<${comp}`);
      expect(mount, `${comp} must be mounted`).toBeGreaterThan(-1);
      const before = s.slice(Math.max(0, mount - 250), mount);
      expect(before, `${comp} must be framed live`)
        .toContain('<ProvenanceFrame provenance="live"');
    }
  });

  it('fleet fixture deployment tiles are framed RED and Settings flags are not green', () => {
    // M-FLEET-2: the fixture fleet is GONE from this view, so its red frame went
    // with it — the approved policy is to remove a red border once the fixture
    // content it marked no longer exists. What must hold now is stronger: the
    // view reads only the authoritative hook, and cannot reach fixture data.
    const fleet = read('views/FleetOverview.tsx');
    expect(fleet).toContain('useOperationalFleet');
    expect(fleet).not.toContain('useFixtureFleetPreview');
    expect(fleet).not.toContain('ProvenanceFrame');
    expect(fleet).not.toContain('provenance="fixture"');
    // The Feature Flags settings section must not inherit runtime-config green.
    const gv = read('views/global/GlobalViews.tsx');
    const flags = gv.indexOf('title="Feature Flags"');
    expect(flags).toBeGreaterThan(-1);
    const before = gv.slice(Math.max(0, flags - 200), flags);
    expect(before, 'flags section must be classified placeholder')
      .toContain('provenance="placeholder"');
  });

  it('fixture fleet and fixture trade cards remain RED', () => {
    // M-FLEET-2: there are no fixture fleet or account cards left to be RED.
    // Both views render authoritative records or an honest empty/unavailable
    // state, so their panels are framed live and the FIXTURE tags are gone.
    const fleet = read('views/FleetOverview.tsx');
    expect(fleet).not.toContain('deployment-fixture-tag-');
    const accounts = read('views/AccountsProtectionView.tsx');
    expect(accounts).not.toContain('account-fixture-tag-');
    expect(accounts).toContain('provenance="live"');
    // Fixture records survive ONLY on the isolated development route.
    const preview = read('views/dev/FixtureFleetPreview.tsx');
    expect(preview).toContain('provenance="fixture"');
    expect(preview).toContain('useFixtureFleetPreview');
    // M-TRADES-1: the trades/orders panels no longer hold fixture records, so
    // their red frames went with the content; the events feed stays red (mixed
    // fixture+runtime, retired by M-EVENTS-1) and other fixture panels remain.
    const pair = read('views/pair/PairViews.tsx');
    expect(pair).toContain('provenance="mixed"');            // events feed stays red
    expect(pair).toContain('useOperationalTrades');
    expect(pair).not.toContain('useTrades(');
  });

  it('risk/accounts, edge monitor, system confidence and broker health stay RED', () => {
    // M-FLEET-2: AccountsProtectionView no longer holds fixture accounts.
    expect(read('views/BrokerHealthView.tsx')).toContain('provenance="fixture"');
    expect(read('views/EdgeMonitorView.tsx')).toContain('provenance="fixture"');
    const sys = read('views/SystemView.tsx');
    expect(sys).toContain('provenance="fixture"');           // system confidence + fixture panels
    expect(sys).toContain('provenance="placeholder"');       // feature flags card
  });

  it('runtime-config settings sections default GREEN and Storage & Feeds is live', () => {
    const gv = read('views/global/GlobalViews.tsx');
    // SettingsSection defaults to runtime-config; only explicit overrides
    // (e.g. the placeholder flags section) may downgrade it.
    expect(gv).toContain("provenance = 'runtime-config'");
    expect(gv).toContain('provenance={provenance}');
    expect(read('views/SystemView.tsx')).toContain('provenance="live"');
  });

  it('M-RISK-1: accounts view renders no fixture funded rules', () => {
    const av = read('views/AccountsProtectionView.tsx');
    expect(av).not.toContain('fundedRules');            // fixture rules unreachable
    expect(av).toContain('funded-rules-unavailable');   // honest state present
    expect(av).toContain('No funded-account rule source is configured');
    // M-FLEET-2: the card is no longer RED because the fixture ACCOUNT it
    // framed is gone; the funded-rule unavailable state above is unchanged.
    // SystemView must not render legacy fabricated limit fields either.
    const sys = read('views/SystemView.tsx');
    for (const legacy of ['maxOpenTrades', 'maxExposureLots', 'maxDailyLoss', 'maxFloatingLoss']) {
      expect(sys, `legacy fabricated limit field ${legacy}`).not.toContain(`riskLimits.${legacy}`);
    }
    expect(sys).toContain('no funded-rule source configured');
  });

  it('M-CONF-1: no fabricated system confidence renders anywhere', () => {
    // Shell header: no score, no band colours, no fabricated explanation.
    const bar = read('components/shell/CommandSafetyBar.tsx');
    expect(bar).not.toContain('confidence.score');
    expect(bar).not.toContain('confidence.band');
    expect(bar).not.toContain('confidence.explanation');
    expect(bar).toContain('system-confidence-not-computed');
    expect(bar).toContain('not computed');
    // Context bar: the three fixture signal chips are gone.
    const ctx = read('components/shell/ContextBar.tsx');
    expect(ctx).not.toContain('CHIP_SIGNALS');
    expect(ctx).not.toContain('fx MD feed');
    // Fleet attention rail: no confidence-derived signals; honest empty state.
    const fleet = read('views/FleetOverview.tsx');
    expect(fleet).not.toContain('confidence.signals');
    expect(fleet).not.toContain('useSystemConfidence');
    expect(fleet).toContain('No attention model');
    // No component anywhere renders a confidence progress bar or score field.
    for (const file of allTsx(SRC)) {
      const text = readFileSync(file, 'utf8');
      expect(text, `${path.relative(SRC, file)} renders confidence.score`)
        .not.toContain('confidence.score');
    }
  });

  it('M-EDGE-1: no fabricated edge/performance metrics render anywhere', () => {
    const REMOVED = ['expectancyR', 'winRate', 'edgeDrift', 'policyHealth',
                     'researchVsLive', 'ghostVsLive', 'distributionDrift',
                     'featureDrift', 'forwardTestHealth', 'operatorConfidence',
                     'futureCandidates'];
    // Main Edge Monitor view: honest state, no metrics, no hook.
    const em = read('views/EdgeMonitorView.tsx');
    for (const f of REMOVED) expect(em, `EdgeMonitorView renders ${f}`).not.toContain(`metrics.${f}`);
    expect(em).not.toContain('useEdgeMonitor');
    expect(em).toContain('edge-monitor-not-computed');
    expect(em).toContain('Edge performance is not computed.');
    // Pair edge tab: kept, honest, no pair-specific claim, no metrics.
    const pv = read('views/pair/PairViews.tsx');
    for (const f of REMOVED) expect(pv, `PairViews renders ${f}`).not.toContain(`metrics.${f}`);
    expect(pv).not.toContain('useEdgeMonitor');
    expect(pv).toContain('pair-edge-monitor-not-computed');
    // Pair Health no longer shows edge-fed expectancy/win rate.
    const health = pv.slice(pv.indexOf('title="Pair Health"'), pv.indexOf('title="Pair Health"') + 900);
    expect(health).not.toContain('Expectancy');
    expect(health).not.toContain('Win rate');
    // Repo-wide: no .tsx consumer reads the removed EdgeMonitor shape.
    for (const file of allTsx(SRC)) {
      const text = readFileSync(file, 'utf8');
      expect(text, `${path.relative(SRC, file)} reads edge.metrics`).not.toContain('edge.metrics');
    }
    // The honest surfaces must NOT read as operationally green.
    expect(em).toContain('provenance="placeholder"');
    expect(pv).toContain('provenance="placeholder"');
  });

  it('chart cards use dynamic candle provenance, never a static green', () => {
    expect(read('views/global/GlobalViews.tsx')).toContain('candleCardProvenance(');
    expect(read('views/pair/PairViews.tsx')).toContain('candleCardProvenance(');
  });
});
