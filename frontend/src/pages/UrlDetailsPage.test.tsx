import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { UrlDetailsPage } from './UrlDetailsPage';
import { errorBody, fakeResponse, makeShortUrl } from '@/test/factories';

function renderDetails(code: string, response: Response | Error) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => (response instanceof Error ? Promise.reject(response) : Promise.resolve(response))),
  );

  render(
    <MemoryRouter initialEntries={[`/urls/${code}`]}>
      <Routes>
        <Route path="/urls/:code" element={<UrlDetailsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('UrlDetailsPage', () => {
  it('shows a loading state while fetching', () => {
    renderDetails('aB3xY7z', fakeResponse({ body: makeShortUrl() }));
    expect(screen.getByText(/loading link details/i)).toBeInTheDocument();
  });

  it('renders every metadata field the brief requires', async () => {
    renderDetails(
      'aB3xY7z',
      fakeResponse({
        body: makeShortUrl({
          short_code: 'aB3xY7z',
          original_url: 'https://example.com/deep/path',
          click_count: 1234,
          expires_at: '2030-01-01T00:00:00Z',
        }),
      }),
    );

    expect(await screen.findByText('aB3xY7z')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'https://example.com/deep/path' })).toBeInTheDocument();
    expect(screen.getByText('1,234')).toBeInTheDocument();
    expect(screen.getByText(/^active$/i)).toBeInTheDocument();
    expect(screen.getByText('Created')).toBeInTheDocument();
    expect(screen.getByText('Expires')).toBeInTheDocument();
  });

  it('opens the destination safely in a new tab', async () => {
    renderDetails('aB3xY7z', fakeResponse({ body: makeShortUrl() }));
    const link = await screen.findByRole('link', { name: /example\.com\/some\/very\/long\/path/i });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(link.getAttribute('rel')).toContain('noreferrer');
  });

  it('distinguishes an expired link from a missing one', async () => {
    renderDetails('aB3xY7z', fakeResponse({ body: makeShortUrl({ status: 'expired' }) }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/this link has expired/i);
    expect(screen.getByText(/410 gone/i)).toBeInTheDocument();
  });

  it('shows a not-found state with a path back for an unknown code', async () => {
    renderDetails(
      'nope123',
      fakeResponse({
        status: 404,
        body: errorBody('not_found', 'No URL exists for that short code.'),
      }),
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(/link not found/i);
    expect(screen.getByRole('link', { name: /create a new link/i })).toHaveAttribute('href', '/');
  });

  it('offers a retry when the backend is degraded', async () => {
    renderDetails('aB3xY7z', new TypeError('Failed to fetch'));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not reach the server/i);
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('handles a rate-limited metadata read', async () => {
    renderDetails(
      'aB3xY7z',
      fakeResponse({
        status: 429,
        body: errorBody('rate_limited', 'Rate limit exceeded. Please slow down and retry shortly.'),
        headers: { 'Retry-After': '5' },
      }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/too many requests/i);
    expect(alert).toHaveTextContent(/try again in 5 seconds/i);
  });
});
