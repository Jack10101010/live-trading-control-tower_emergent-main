/**
 * UI-9 — the security panel must display STATUS ONLY.
 *
 * The assertions are mostly negative, because the risk this panel carries is
 * displaying something it should not: a token, a certificate path, an endpoint
 * hostname. The backend never sends values, so these tests also serve as a
 * canary — if a future change starts returning values, they fail here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { SecurityConfigStatus } from '@/lib/api';
import { api } from '@/lib/api';
import { SecurityBaselinePanel } from '@/components/domain/SecurityBaselinePanel';

const SECRET = 'tok_live_MUST_NOT_RENDER';
const HOSTNAME = 'vps.internal.example';

function payload(over: Partial<SecurityConfigStatus> = {}): SecurityConfigStatus {
  return {
    mode: 'missing',
    ingestAuth: {
      enabled: false, enforcing: false, misconfigured: false, tokenPresent: false,
      issueCodes: [], routes: ['/api/live/ingest'],
      legacyOperatorTokenAllowed: false, degraded: false,
    },
    connectivity: {
      profile: 'local_loopback',
      approvedProfiles: ['local_loopback'],
      remoteApproved: false,
      transport: { enabled: false, selectedKind: 'null', misconfigured: false, reason: 'transport_not_enabled' },
      connectionPolicy: { allowed: false, reason: 'no_endpoint_configured', profile: 'local_loopback' },
      outboundNodeAuth: { tokenPresent: false },
      missingRemotePrerequisites: ['tls_trust_configured'],
      localOnly: true,
    },
    variables: {
      CONTROL_TOWER_MODE: { status: 'missing', secret: false, path: false },
      NODE_ENDPOINT: { status: 'missing', secret: false, path: false },
      NODE_API_TOKEN: { status: 'missing', secret: true, path: false },
      NODE_CLIENT_KEY: { status: 'missing', secret: true, path: true },
    },
    findings: [],
    hasErrors: false,
    auth: {
      active: false,
      configured: false,
      defaultState: 'disabled',
      scheme: 'bearer',
      valid: true,
      misconfigured: false,
      tokenPresent: false,
      tokenLengthOk: false,
      minTokenLength: 32,
      protectedRouteCount: 68,
      publicRouteCount: 1,
      publicRoutes: ['/api/health'],
      docsPolicy: 'protected',
      openapiPolicy: 'protected',
      issueCodes: [],
      remoteActivation: 'not_active',
    },
    cors: {
      policyActive: true,
      source: 'safe_default',
      originCount: 3,
      credentialsEnabled: false,
      allowedMethods: ['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT'],
      allowedHeaders: ['Accept', 'Content-Type', 'Idempotency-Key'],
      localOnly: true,
      wildcardEnabled: false,
      valid: true,
      issueCodes: [],
    },
    ...over,
  };
}

function cors(over: Partial<NonNullable<SecurityConfigStatus['cors']>> = {}) {
  return payload({ cors: { ...payload().cors!, ...over } });
}

function auth(over: Partial<NonNullable<SecurityConfigStatus['auth']>> = {}) {
  return payload({ auth: { ...payload().auth!, ...over } });
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SecurityBaselinePanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('activation state', () => {
  it('reports LOCAL-ONLY with a truthful connectivity summary', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    const summary = await screen.findByTestId('connectivity-summary');
    expect(summary.textContent).toContain('local_loopback');
    expect(summary.textContent).toContain('transport disabled');
    expect(summary.textContent).toContain('ingest auth disabled');
    expect(screen.getByTestId('security-active-state').textContent).toBe('LOCAL-ONLY');
  });

  it('reports enabled-but-misconfigured transport distinctly and degraded legacy compat', async () => {
    const p = payload();
    p.connectivity!.transport = { enabled: true, selectedKind: 'null', misconfigured: true, reason: 'enabled_but_configuration_invalid' };
    p.ingestAuth!.legacyOperatorTokenAllowed = true;
    p.ingestAuth!.degraded = true;
    vi.spyOn(api, 'securityConfig').mockResolvedValue(p);
    mount();
    const summary = await screen.findByTestId('connectivity-summary');
    expect(summary.textContent).toContain('enabled but misconfigured');
    expect(summary.textContent).toContain('DEGRADED');
  });

  it('never claims remote approval, however much is configured', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      payload({
        variables: {
          NODE_ENDPOINT: { status: 'configured', secret: false, path: false },
          NODE_API_TOKEN: { status: 'configured', secret: true, path: false },
          NODE_CLIENT_KEY: { status: 'configured', secret: true, path: true },
        },
      })
    );
    mount();
    await waitFor(() =>
      expect(screen.getByTestId('security-active-state').textContent).toBe('LOCAL-ONLY')
    );
  });
});

describe('status display', () => {
  it('shows configured / missing per variable', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      payload({
        variables: {
          NODE_ENDPOINT: { status: 'configured', secret: false, path: false },
          NODE_API_TOKEN: { status: 'missing', secret: true, path: false },
        },
      })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId('security-variables')).toBeTruthy());
    const text = screen.getByTestId('security-variables').textContent ?? '';
    expect(text).toContain('NODE_ENDPOINT');
    expect(text).toContain('configured');
    expect(text).toContain('NODE_API_TOKEN');
    expect(text).toContain('missing');
  });

  it('marks secret variables as never displayed', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('security-variables')).toBeTruthy());
    expect(screen.getByTestId('security-variables').textContent).toMatch(
      /secret — never displayed/
    );
  });

  it('distinguishes invalid from missing', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      payload({
        variables: { NODE_CONNECT_TIMEOUT: { status: 'invalid', secret: false, path: false } },
        findings: [{ severity: 'error', variable: 'NODE_CONNECT_TIMEOUT',
                     message: 'NODE_CONNECT_TIMEOUT could not be parsed.' }],
        hasErrors: true,
      })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId('security-findings')).toBeTruthy());
    expect(screen.getByTestId('security-variables').textContent).toContain('invalid');
    expect(screen.getByTestId('security-findings').textContent).toMatch(/could not be parsed/);
  });
});

describe('no value ever renders', () => {
  it('cannot display a token even if one is somehow present in the payload', async () => {
    // Deliberately hostile: an extra field the type does not allow. The component
    // renders only `status`, so a rogue value cannot reach the DOM.
    vi.spyOn(api, 'securityConfig').mockResolvedValue({
      ...payload(),
      variables: {
        NODE_API_TOKEN: {
          status: 'configured', secret: true, path: false,
          value: SECRET, endpoint: HOSTNAME,
        } as never,
      },
    });
    mount();
    await waitFor(() => expect(screen.getByTestId('security-variables')).toBeTruthy());
    const html = screen.getByTestId('security-baseline-panel').innerHTML;
    expect(html).not.toContain(SECRET);
    expect(html).not.toContain(HOSTNAME);
  });
});

describe('degradation', () => {
  it('says status unavailable when the backend cannot be reached', async () => {
    vi.spyOn(api, 'securityConfig').mockRejectedValue(new Error('failed to fetch'));
    mount();
    expect(await screen.findByText(/status unavailable/i)).toBeTruthy();
    // An unreachable backend must not read as ACTIVE.
    expect(screen.getByTestId('security-active-state').textContent).not.toBe('REMOTE APPROVED');
  });
});

describe('read-only boundary', () => {
  it('renders no control of any kind', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('security-variables')).toBeTruthy());
    const panel = screen.getByTestId('security-baseline-panel');
    expect(panel.querySelectorAll('button')).toHaveLength(0);
    expect(panel.querySelectorAll('input')).toHaveLength(0);
    expect(panel.querySelectorAll('form')).toHaveLength(0);
    expect(panel.querySelectorAll('a[href]')).toHaveLength(0);
  });
});

// ══ UI-10: browser origin policy ═════════════════════════════════════════════

describe('CORS policy status', () => {
  it('renders the safe local default', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('cors-policy')).toBeTruthy());
    expect(screen.getByTestId('cors-source').textContent).toBe('safe_default');
    expect(screen.getByTestId('cors-scope').textContent).toBe('local only');
    expect(screen.getByTestId('cors-origin-count').textContent).toBe('3');
    expect(screen.getByTestId('cors-validation').textContent).toBe('valid');
  });

  it('shows wildcard disabled and credentials disabled', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('cors-policy')).toBeTruthy());
    expect(screen.getByTestId('cors-wildcard').textContent).toBe('disabled');
    expect(screen.getByTestId('cors-credentials').textContent).toBe('disabled');
  });

  it('classifies an explicitly configured external policy', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      cors({ source: 'explicit', localOnly: false, originCount: 1 })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId('cors-source').textContent).toBe('explicit'));
    expect(screen.getByTestId('cors-scope').textContent).toBe('externally configured');
  });

  it('warns explicitly on an invalid fallback without implying broader access', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      cors({ source: 'invalid_fallback', valid: false,
             issueCodes: ['origin_wildcard_unsupported'] })
    );
    mount();
    expect(await screen.findByText(/no entry was usable/i)).toBeTruthy();
    expect(screen.getByText(/access was not broadened/i)).toBeTruthy();
  });

  it('lists issue codes verbatim', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      cors({ valid: false, issueCodes: ['origin_contains_path', 'credentials_flag_invalid'] })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId('cors-issues')).toBeTruthy());
    const text = screen.getByTestId('cors-issues').textContent ?? '';
    expect(text).toContain('origin_contains_path');
    expect(text).toContain('credentials_flag_invalid');
  });

  it('states that CORS is not authentication', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    expect(await screen.findByText(/not authentication/i)).toBeTruthy();
  });

  it('renders no origin editor and no action control', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('cors-policy')).toBeTruthy());
    const section = screen.getByTestId('cors-policy');
    expect(section.querySelectorAll('button')).toHaveLength(0);
    expect(section.querySelectorAll('input')).toHaveLength(0);
    expect(section.querySelectorAll('select')).toHaveLength(0);
    expect(section.querySelectorAll('textarea')).toHaveLength(0);
  });

  it('cannot render an origin string even if the payload smuggles one', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue({
      ...payload(),
      cors: { ...payload().cors!, origins: ['https://secret.internal.example'] } as never,
    });
    mount();
    await waitFor(() => expect(screen.getByTestId('cors-policy')).toBeTruthy());
    expect(screen.getByTestId('security-baseline-panel').innerHTML)
      .not.toContain('secret.internal.example');
  });

  it('omits the section entirely when the backend sends no cors block', async () => {
    const { cors: _omitted, ...withoutCors } = payload();
    vi.spyOn(api, 'securityConfig').mockResolvedValue(withoutCors as SecurityConfigStatus);
    mount();
    await waitFor(() => expect(screen.getByTestId('security-variables')).toBeTruthy());
    expect(screen.queryByTestId('cors-policy')).toBeNull();
  });
});

// ══ UI-11: request authentication ════════════════════════════════════════════

describe('authentication diagnostics', () => {
  it('renders and reports NOT ACTIVE by default', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('auth-policy')).toBeTruthy());
    expect(screen.getByTestId('auth-active').textContent).toBe('NOT ACTIVE');
    expect(screen.getByTestId('auth-default').textContent).toBe('disabled');
    expect(screen.getByTestId('auth-configured').textContent).toBe('no');
  });

  it('shows the bearer scheme and route counts', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('auth-policy')).toBeTruthy());
    expect(screen.getByTestId('auth-scheme').textContent).toBe('bearer');
    expect(screen.getByTestId('auth-protected-count').textContent).toBe('68');
    expect(screen.getByTestId('auth-public-count').textContent).toBe('1');
    expect(screen.getByTestId('auth-docs-policy').textContent).toBe('protected / protected');
  });

  it('reports a configured but inactive policy', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      auth({ configured: true, tokenPresent: true, tokenLengthOk: true, active: false })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId('auth-configured').textContent).toBe('yes'));
    expect(screen.getByTestId('auth-active').textContent).toBe('NOT ACTIVE');
    expect(screen.getByTestId('auth-token-present').textContent).toBe('present');
  });

  it('warns on a misconfigured policy and says access was not reopened', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      auth({ configured: true, misconfigured: true, valid: false,
             issueCodes: ['auth_token_missing'] })
    );
    mount();
    expect(await screen.findByText(/failing closed/i)).toBeTruthy();
    expect(screen.getByText(/not silently reopened/i)).toBeTruthy();
    expect(screen.getByTestId('auth-issues').textContent).toContain('auth_token_missing');
  });

  it('indicates a too-short token without revealing its length', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(
      auth({ tokenPresent: true, tokenLengthOk: false })
    );
    mount();
    await waitFor(() =>
      expect(screen.getByTestId('auth-token-present').textContent).toBe('present · too short')
    );
  });

  it('states that authentication is not encryption', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    expect(await screen.findByText(/not encryption/i)).toBeTruthy();
  });

  it('cannot render a token even if the payload smuggles one', async () => {
    const SECRET_TOKEN = 'tok_MUST_NEVER_RENDER_0123456789';
    vi.spyOn(api, 'securityConfig').mockResolvedValue({
      ...payload(),
      auth: { ...payload().auth!, token: SECRET_TOKEN, tokenValue: SECRET_TOKEN,
              tokenLength: 31 } as never,
    });
    mount();
    await waitFor(() => expect(screen.getByTestId('auth-policy')).toBeTruthy());
    const html = screen.getByTestId('security-baseline-panel').innerHTML;
    expect(html).not.toContain(SECRET_TOKEN);
    expect(html).not.toContain('tok_');
  });

  it('renders no login form, token field, storage control or action button', async () => {
    vi.spyOn(api, 'securityConfig').mockResolvedValue(payload());
    mount();
    await waitFor(() => expect(screen.getByTestId('auth-policy')).toBeTruthy());
    const section = screen.getByTestId('auth-policy');
    expect(section.querySelectorAll('form')).toHaveLength(0);
    expect(section.querySelectorAll('input')).toHaveLength(0);
    expect(section.querySelectorAll('button')).toHaveLength(0);
    expect(section.querySelectorAll('select')).toHaveLength(0);
    expect(section.querySelectorAll('[type="password"]')).toHaveLength(0);
  });

  it('omits the section when the backend sends no auth block', async () => {
    const { auth: _omitted, ...withoutAuth } = payload();
    vi.spyOn(api, 'securityConfig').mockResolvedValue(withoutAuth as SecurityConfigStatus);
    mount();
    await waitFor(() => expect(screen.getByTestId('security-variables')).toBeTruthy());
    expect(screen.queryByTestId('auth-policy')).toBeNull();
  });
});
