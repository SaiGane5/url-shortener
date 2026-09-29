import type { ShortUrl } from '@/api/types';

/** Builds a `ShortUrl` with sane defaults; override only what a test cares about. */
export function makeShortUrl(overrides: Partial<ShortUrl> = {}): ShortUrl {
  return {
    short_code: 'aB3xY7z',
    short_url: 'http://localhost:8080/aB3xY7z',
    original_url: 'https://example.com/some/very/long/path?utm_source=test',
    created_at: '2026-09-04T10:00:00Z',
    updated_at: '2026-09-04T10:00:00Z',
    expires_at: null,
    click_count: 0,
    status: 'active',
    ...overrides,
  };
}

interface FakeResponseInit {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

/**
 * Minimal `Response` stand-in for `fetch` mocks.
 *
 * A real `Response` cannot carry an arbitrary status/body pair as ergonomically
 * (204 with a body throws), and tests need to assert on our header parsing, so
 * this exposes exactly the surface `client.ts` touches.
 */
export function fakeResponse({ status = 200, body, headers = {} }: FakeResponseInit = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    json: () =>
      body === undefined ? Promise.reject(new SyntaxError('no body')) : Promise.resolve(body),
  } as unknown as Response;
}

/** Error envelope in the exact shape the backend returns. */
export function errorBody(
  code: string,
  message: string,
  extras: { fields?: { field: string; message: string }[]; requestId?: string } = {},
) {
  return {
    error: {
      code,
      message,
      ...(extras.fields ? { details: { fields: extras.fields } } : {}),
      ...(extras.requestId ? { request_id: extras.requestId } : {}),
    },
  };
}
