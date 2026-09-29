import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { HomePage } from './HomePage';
import { errorBody, fakeResponse, makeShortUrl } from '@/test/factories';

/**
 * These are the end-to-end frontend tests: they drive the real form, the real
 * hook, and the real API client, stubbing only `fetch`. That keeps them honest
 * about the thing that actually breaks in practice -- how a server response is
 * turned into what the user sees.
 */

interface Route {
  match: (url: string, method: string) => boolean;
  response: Response | Error;
}

function stubRoutes(routes: Route[]) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof URL ? input.href : typeof input === 'string' ? input : input.url;
    const method = init?.method ?? 'GET';
    const route = routes.find((candidate) => candidate.match(url, method));
    if (!route) return Promise.reject(new TypeError(`unstubbed request: ${method} ${url}`));
    return route.response instanceof Error
      ? Promise.reject(route.response)
      : Promise.resolve(route.response);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const emptyListRoute: Route = {
  match: (url, method) => method === 'GET' && url.includes('/api/v1/urls'),
  response: fakeResponse({ body: { items: [], next_cursor: null } }),
};

function renderHome() {
  render(
    <MemoryRouter>
      <HomePage />
    </MemoryRouter>,
  );
  return userEvent.setup();
}

describe('HomePage', () => {
  it('shows the empty state for recent links before any exist', async () => {
    stubRoutes([emptyListRoute]);
    renderHome();

    expect(await screen.findByText(/no links yet/i)).toBeInTheDocument();
  });

  it('creates a link and displays the generated short URL', async () => {
    const created = makeShortUrl({
      short_code: 'zZ9aB1c',
      short_url: 'http://localhost:8080/zZ9aB1c',
    });
    stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: fakeResponse({ status: 201, body: created }),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://example.com/very/long');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(await screen.findByRole('heading', { name: /your short link is ready/i })).toBeVisible();
    expect(screen.getByRole('link', { name: 'http://localhost:8080/zZ9aB1c' })).toHaveAttribute(
      'href',
      'http://localhost:8080/zZ9aB1c',
    );
    expect(screen.getByRole('link', { name: /view details/i })).toHaveAttribute(
      'href',
      '/urls/zZ9aB1c',
    );
  });

  it('sends the destination URL in the request body', async () => {
    const fetchMock = stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: fakeResponse({ status: 201, body: makeShortUrl() }),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://example.com/x');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST');
      expect(post).toBeDefined();
      const rawBody = post?.[1]?.body;
      expect(typeof rawBody).toBe('string');
      expect(JSON.parse(rawBody as string)).toEqual({ url: 'https://example.com/x' });
    });
  });

  it('renders the backend validation message when the API rejects the URL', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: fakeResponse({
          status: 422,
          body: errorBody(
            'private_target',
            'URLs pointing at private, loopback, or internal addresses are not allowed.',
            { requestId: 'req-abc' },
          ),
        }),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://internal.example.com');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/private, loopback, or internal addresses/i);
    expect(alert).toHaveTextContent(/req-abc/);
  });

  it('handles a 429 by showing the limit message and the retry delay', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: fakeResponse({
          status: 429,
          body: errorBody(
            'rate_limited',
            'Rate limit exceeded. Please slow down and retry shortly.',
          ),
          headers: {
            'Retry-After': '30',
            'X-RateLimit-Limit': '20',
            'X-RateLimit-Remaining': '0',
            'X-RateLimit-Policy': '20;w=60;burst=20;policy=write;algorithm=token_bucket',
          },
        }),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/too many requests/i);
    expect(alert).toHaveTextContent(/try again in 30 seconds/i);
    expect(alert).toHaveTextContent(/policy=write/);
  });

  it('reports a degraded backend when the request cannot be delivered', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: new TypeError('Failed to fetch'),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not reach the server/i);
  });

  it('reports a 503 when a backend dependency is down, with a retry affordance', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: fakeResponse({
          status: 503,
          body: errorBody(
            'dependency_unavailable',
            'Rate limiting is temporarily unavailable, so write requests are being rejected.',
          ),
        }),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/temporarily unavailable/i);
    expect(within(alert).getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('warns when the remaining request budget is nearly exhausted', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'POST' && url.includes('/api/v1/urls'),
        response: fakeResponse({
          status: 201,
          body: makeShortUrl(),
          headers: { 'X-RateLimit-Remaining': '2', 'X-RateLimit-Reset': '45' },
        }),
      },
      emptyListRoute,
    ]);

    const user = renderHome();
    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(await screen.findByText(/approaching the rate limit/i)).toBeInTheDocument();
    expect(screen.getByText(/2 requests left in this window/i)).toBeInTheDocument();
  });

  it('lists previously created links', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'GET' && url.includes('/api/v1/urls'),
        response: fakeResponse({
          body: {
            items: [makeShortUrl({ short_code: 'abc1234' })],
            next_cursor: null,
          },
        }),
      },
    ]);

    renderHome();
    expect(await screen.findByRole('link', { name: '/abc1234' })).toBeInTheDocument();
  });

  it('does not block the form when the recent-links panel fails', async () => {
    stubRoutes([
      {
        match: (url, method) => method === 'GET' && url.includes('/api/v1/urls'),
        response: fakeResponse({ status: 500, body: errorBody('internal_error', 'boom') }),
      },
    ]);

    renderHome();
    expect(await screen.findByText(/could not load recent links/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/long url/i)).toBeEnabled();
  });
});
