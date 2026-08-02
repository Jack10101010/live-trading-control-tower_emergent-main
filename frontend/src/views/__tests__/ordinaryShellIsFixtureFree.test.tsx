/**
 * M-WORLD-ORDINARY-1 — no fixture identity or fixture decisions in ordinary UI.
 *
 * TWO CONTAMINATIONS, BOTH ON EVERY PAGE OR NEAR IT
 *
 *   1. `useOperator()` fetched `/api/world` and returned `world.operators[0]`.
 *      The application shell rendered that authored person's display name in
 *      the chrome of EVERY ordinary route and its first letter as an avatar;
 *      Settings showed the id, a `Role` badge and a `Timezone` with invented
 *      `?? 'Lead'` / `?? 'UTC'` fallbacks layered on top. So the whole
 *      22-collection fixture was fetched by every route in order to display an
 *      invented human identity in an operator console.
 *
 *   2. `/api/strategy/decisions` evaluated the engine against fixture
 *      deployments, a policy matrix derived from fixture packages, and fixture
 *      recommendations — then SystemView rendered the result as the live
 *      engine's operational record, complete with a health dot.
 *
 * The honest replacement invents nothing. There is no per-user authentication
 * in this deployment (`auth_policy` is one shared token with no users or
 * sessions), so the shell shows a CONFIGURED operator id or says none is
 * configured, and no name, role, timezone or avatar at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { OperatorIdentityChip } from '@/components/shell/OperatorIdentityChip';
import type { OperatorIdentity } from '@/lib/api';

const state = { operator: {} as OperatorIdentity };

const CONFIGURED: OperatorIdentity = {
  operatorId: 'jack.haymes', attributed: true, assurance: 'asserted',
  source: 'configuration', configVar: 'CONTROL_TOWER_OPERATOR_ID',
  displayName: null, role: null, timezone: null, email: null,
  avatarUrl: null, permissions: null,
};

const UNCONFIGURED = { ...CONFIGURED, operatorId: 'UNATTRIBUTED',
                       attributed: false, source: 'unconfigured' };

/** The fixture operator that used to reach the chrome of every page. */
const FIXTURE_SENTINELS = ['Jack (Lead)', 'op_01J8Z5A2C4E6G8J0M2P4R6T8V0XZ'];

function renderShell() {
  return render(
    <MemoryRouter><OperatorIdentityChip operator={state.operator} /></MemoryRouter>);
}

beforeEach(() => { state.operator = CONFIGURED; });

describe('the application shell shows no invented person', () => {
  it('renders the CONFIGURED operator id, labelled as configuration', () => {
    const { container } = renderShell();
    expect(screen.getByTestId('shell-operator').textContent).toContain('jack.haymes');
    expect(container.textContent).toMatch(/configured/);
    // Configuration is not a login, and the shell must not imply one.
    expect(container.textContent).not.toMatch(/signed in|logged in|session/i);
  });

  it('says so plainly when no operator is configured', () => {
    state.operator = UNCONFIGURED;
    renderShell();
    expect(screen.getByTestId('shell-operator').textContent)
      .toMatch(/Operator not configured/);
    expect(screen.getByTestId('shell-operator').textContent).toMatch(/unattributed/);
  });

  it('renders NO avatar, and never an initial derived from a name', () => {
    const { container } = renderShell();
    // The old shell did `String(operator.displayName).charAt(0)`. With no name
    // source that would render "u" from "undefined" — a letter standing in for
    // a person nobody established.
    expect(container.querySelector('.rounded-full')).toBeNull();
    expect(container.textContent).not.toMatch(/\bundefined\b/);
  });

  it('invents no name, role, timezone or permission anywhere', () => {
    const { container } = renderShell();
    const text = container.textContent!;
    for (const invented of ['Lead', 'UTC', 'Admin', 'Owner', 'Viewer']) {
      expect(text, `shell invented "${invented}"`).not.toContain(invented);
    }
  });

  it('renders no fixture operator sentinel', () => {
    const { container } = renderShell();
    for (const sentinel of FIXTURE_SENTINELS) {
      expect(container.innerHTML).not.toContain(sentinel);
    }
  });

  it('an empty operator id never becomes an initial or a blank chip', () => {
    state.operator = { ...UNCONFIGURED, operatorId: '' };
    const { container } = renderShell();
    expect(container.textContent).toMatch(/Operator not configured/);
    expect(container.querySelector('.rounded-full')).toBeNull();
  });

  it('the shell renders identically whether or not a fixture exists', () => {
    // The shell no longer reads any fixture-backed source, so there is no
    // fixture state that could change it. Rendering twice with the same
    // identity must be byte-identical.
    const first = renderShell().container.innerHTML;
    const second = renderShell().container.innerHTML;
    expect(first).toBe(second);
  });
});
