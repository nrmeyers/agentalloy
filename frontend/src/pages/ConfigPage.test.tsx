import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigPage } from './ConfigPage';
import { ToastContainer } from '../components/Toast';
import {
  ApiError,
  getConfig,
  getRepos,
  getUpstream,
  updateUpstream,
  updateConfig,
  reloadConfig,
} from '../lib/api';
import type { ConfigData, ReposResponse, UpstreamConfig } from '../lib/types';

// Mock the API boundary so the real hooks (react-query keys, invalidation, toast)
// are exercised without a network. Non-GET CSRF handling lives in request(),
// which is below this boundary — the hooks are the unit under test here.
vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
    reloadConfig: vi.fn(),
    getRepos: vi.fn(),
    getUpstream: vi.fn(),
    updateUpstream: vi.fn(),
  };
});

const config: ConfigData = {
  upstream_url: 'https://global.example.com',
  upstream_model: 'global-model',
  upstream_api_key: null,
  anthropic_upstream_url: '',
  runtime_embed_base_url: 'http://localhost:47951',
  runtime_embedding_model: 'embed-model',
  embedding_provider: 'local',
  log_level: 'INFO',
  dedup_hard_threshold: 0.8,
  dedup_soft_threshold: 0.5,
  sdd_fast_require_approval: false,
  profile_root: '/profiles',
  forced_profile: null,
  duckdb_path: '/data/duck.duck',
  fragments_lance_path: '/data/fragments.lance',
  telemetry_db_path: '/data/telemetry.duck',
  env_file_path: '/home/user/.env',
};

const repos: ReposResponse = {
  total: 2,
  repos: [
    {
      repo_root: '/home/user/repo-a',
      harnesses: ['qwen-code'],
      exists: true,
      phase: 'build',
      lifecycle_mode: 'full',
      profile: null,
      upstream_url: null,
      upstream_model: null,
      cursor: null,
      contracts_by_phase: {},
      approval_required: false,
      approval_pending: false,
    },
    {
      repo_root: '/home/user/repo-b',
      harnesses: ['qwen-code'],
      exists: true,
      phase: 'spec',
      lifecycle_mode: 'full',
      profile: null,
      upstream_url: null,
      upstream_model: null,
      cursor: null,
      contracts_by_phase: {},
      approval_required: false,
      approval_pending: false,
    },
  ],
};

const upstreamA: UpstreamConfig = {
  repo_root: '/home/user/repo-a',
  exists: true,
  harness: 'qwen-code',
  url: 'https://orig.example.com',
  model: 'orig-model',
  key_env: 'ORIG_KEY',
  detail: null,
};

/**
 * Fresh QueryClient per render so cached state never leaks between cases.
 * Waits for the config query to resolve so the page has left its skeleton and
 * the global + per-repo cards are mounted.
 */
async function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <ConfigPage />
      <ToastContainer />
    </QueryClientProvider>,
  );
  await screen.findByText('Configuration');
  return utils;
}

/** The per-repo card element (the heading is a direct child of the Card div). */
function perRepoCard(): HTMLElement {
  return screen.getByText('Per-Repo Upstream').closest('div') as HTMLElement;
}

/**
 * Wait for the repo dropdown (from useRepos) to populate, then select a repo.
 * The repo list loads asynchronously, so the option may not exist yet.
 */
async function selectRepo(user: ReturnType<typeof userEvent.setup>, repoRoot: string) {
  await screen.findByRole('option', { name: repoRoot });
  const select = perRepoCard().querySelector('select') as HTMLSelectElement;
  await user.selectOptions(select, repoRoot);
}

/** The global "Upstream LLM" card's first input = the Upstream URL field. */
function globalUrlInput(): HTMLInputElement {
  const card = screen.getByText('Upstream LLM').closest('div') as HTMLElement;
  return card.querySelector('input') as HTMLInputElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getConfig).mockResolvedValue(config);
  vi.mocked(getRepos).mockResolvedValue(repos);
  vi.mocked(reloadConfig).mockResolvedValue({ status: 'ok', message: 'reloaded' });
  vi.mocked(updateConfig).mockResolvedValue({
    status: 'ok',
    message: 'saved',
    env_file_path: '/home/user/.env',
  });
  vi.mocked(getUpstream).mockResolvedValue(upstreamA);
  vi.mocked(updateUpstream).mockResolvedValue({
    status: 'ok',
    repo_root: '/home/user/repo-a',
    harness: 'qwen-code',
  });
});

describe('Per-repo Upstream section (/config)', () => {
  // TP-14 (AC-1): card renders below the global card, with a repo dropdown
  // listing the wired repos from GET /api/repos.
  it('renders a Per-repo Upstream card below the global Upstream LLM card with a repo dropdown', async () => {
    await renderPage();

    const globalH = screen.getByText('Upstream LLM');
    const perRepoH = await screen.findByText('Per-Repo Upstream');
    // The per-repo card must come AFTER the global card in document order.
    expect(globalH.compareDocumentPosition(perRepoH) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const select = perRepoCard().querySelector('select') as HTMLSelectElement;
    expect(select).not.toBeNull();
    expect(screen.getByText('Select a repo…')).toBeInTheDocument();
    // Wired repos from GET /api/repos are listed by repo_root (async load).
    expect(await screen.findByRole('option', { name: '/home/user/repo-a' })).toBeInTheDocument();
    expect(await screen.findByRole('option', { name: '/home/user/repo-b' })).toBeInTheDocument();
  });

  // TP-15 (AC-2): selecting a repo fetches and displays its url/model/key_env.
  it('loads the selected repo url/model/key_env into the form fields', async () => {
    const user = userEvent.setup();
    await renderPage();

    await selectRepo(user, '/home/user/repo-a');

    expect(vi.mocked(getUpstream)).toHaveBeenCalledWith('/home/user/repo-a');
    await waitFor(() => {
      expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('https://orig.example.com');
      expect(screen.getByPlaceholderText('model')).toHaveValue('orig-model');
      expect(screen.getByPlaceholderText('env var name')).toHaveValue('ORIG_KEY');
    });
  });

  // TP-16 (AC-6): absent upstream -> empty state; malformed file -> detail in an
  // error state; neither case crashes the page.
  it('shows an empty state when the repo has no upstream file', async () => {
    const user = userEvent.setup();
    vi.mocked(getUpstream).mockResolvedValue({
      ...upstreamA,
      exists: false,
      harness: null,
      url: null,
      model: null,
      key_env: null,
    });
    await renderPage();

    await selectRepo(user, '/home/user/repo-a');

    expect(await screen.findByText('No per-repo upstream for this repo.')).toBeInTheDocument();
    // No form fields rendered for the absent case.
    expect(screen.queryByPlaceholderText('upstream URL')).not.toBeInTheDocument();
  });

  it('shows the detail string when the upstream file is malformed, without crashing', async () => {
    const user = userEvent.setup();
    vi.mocked(getUpstream).mockResolvedValue({
      ...upstreamA,
      exists: true,
      detail: 'malformed YAML: could not parse line 3',
    });
    await renderPage();

    await selectRepo(user, '/home/user/repo-a');

    expect(
      await screen.findByText('malformed YAML: could not parse line 3'),
    ).toBeInTheDocument();
    // The page still renders (no crash) — the repo dropdown is intact.
    expect(perRepoCard().querySelector('select')).not.toBeNull();
  });

  // TP-17 (AC-3): editing fields + Save issues the PUT with the edited body; on
  // success a toast is shown and the form reflects the saved values.
  it('saves the edited fields via PUT and shows a success toast', async () => {
    const user = userEvent.setup();
    await renderPage();

    await selectRepo(user, '/home/user/repo-a');
    await waitFor(() => expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('https://orig.example.com'));

    await user.clear(screen.getByPlaceholderText('upstream URL'));
    await user.type(screen.getByPlaceholderText('upstream URL'), 'https://new.example.com');
    await user.click(screen.getByRole('button', { name: 'Save Upstream' }));

    // PUT issued with the edited url and the untouched model/key_env.
    expect(vi.mocked(updateUpstream)).toHaveBeenCalledWith('/home/user/repo-a', {
      url: 'https://new.example.com',
      model: 'orig-model',
      key_env: 'ORIG_KEY',
    });
    // Success toast from the mutation's onSuccess.
    expect(await screen.findByText('Upstream saved')).toBeInTheDocument();
    // Form reflects the saved value.
    expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('https://new.example.com');
  });

  // TP-18 (AC-7): a 400 save surfaces the field error in the form and preserves
  // the values for retry.
  it('surfaces a 400 field error and preserves the form values for retry', async () => {
    const user = userEvent.setup();
    vi.mocked(updateUpstream).mockRejectedValue(
      new ApiError(
        'url and model must be non-empty',
        400,
        { detail: { error: 'invalid_field', detail: 'url and model must be non-empty' } },
      ),
    );
    await renderPage();

    await selectRepo(user, '/home/user/repo-a');
    await waitFor(() => expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('https://orig.example.com'));

    await user.clear(screen.getByPlaceholderText('upstream URL'));
    await user.click(screen.getByRole('button', { name: 'Save Upstream' }));

    // The field error from the 400 body is shown in the form.
    expect(await screen.findByText('url and model must be non-empty')).toBeInTheDocument();
    // Values preserved for retry: the (cleared) url stays empty, model untouched.
    expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('');
    expect(screen.getByPlaceholderText('model')).toHaveValue('orig-model');
  });

  // TP-19 (AC-5): key_env is rendered as a plain env-var-name field; the secret
  // is never displayed.
  it('shows key_env as a plain env-var name and never renders a secret', async () => {
    const user = userEvent.setup();
    vi.mocked(getUpstream).mockResolvedValue({ ...upstreamA, key_env: 'MY_API_KEY' });
    await renderPage();

    await selectRepo(user, '/home/user/repo-a');

    const keyEnv = await screen.findByPlaceholderText('env var name');
    // The env-var NAME is shown, in a plain text field (not a masked secret field).
    expect(keyEnv).toHaveValue('MY_API_KEY');
    expect(keyEnv).toHaveAttribute('type', 'text');
    // The secret itself is never present: no input in the section holds it, and
    // no text node in the section renders it.
    const inputs = Array.from(perRepoCard().querySelectorAll('input')) as HTMLInputElement[];
    expect(inputs.some((i) => i.value === 'sk-super-secret-value')).toBe(false);
    expect(perRepoCard().textContent ?? '').not.toContain('sk-super-secret-value');
  });

  // TP-20 (AC-8): the global Upstream LLM card is unchanged, and the two forms
  // are independent — editing one does not alter the other's state.
  it('keeps the global Upstream LLM card and the per-repo form independent', async () => {
    const user = userEvent.setup();
    await renderPage();

    // Global card renders with its original value.
    expect(screen.getByText('Upstream LLM')).toBeInTheDocument();
    expect(globalUrlInput()).toHaveValue('https://global.example.com');

    await selectRepo(user, '/home/user/repo-a');
    await waitFor(() => expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('https://orig.example.com'));

    // Editing the per-repo form does not alter the global form state.
    await user.clear(screen.getByPlaceholderText('upstream URL'));
    await user.type(screen.getByPlaceholderText('upstream URL'), 'https://per-repo-only.example.com');
    expect(globalUrlInput()).toHaveValue('https://global.example.com');

    // ...and vice versa: editing the global form does not alter the per-repo form.
    await user.clear(globalUrlInput());
    await user.type(globalUrlInput(), 'https://global-only.example.com');
    expect(screen.getByPlaceholderText('upstream URL')).toHaveValue('https://per-repo-only.example.com');
  });
});
