import { describe, expect, it, vi } from 'vitest';
import { readRateLimit, request } from './client';
import { ApiError } from './errors';
import { errorBody, fakeResponse, makeShortUrl } from '@/test/factories';

function stubFetch(response: Response | Error) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('readRateLimit', () => {
  it('parses the rate-limit headers the backend sends', () => {
    const info = readRateLimit(
      new Headers({
        'X-RateLimit-Limit': '20',
        'X-RateLimit-Remaining': '3',
        'X-RateLimit-Reset': '42',
        'Retry-After': '7',
        'X-RateLimit-Policy': '20;w=60;burst=20;policy=write;algorithm=token_bucket',
      }),
    );

    expect(info).toEqual({
      limit: 20,
      remaining: 3,
      resetSeconds: 42,
      retryAfterSeconds: 7,
      policy: '20;w=60;burst=20;policy=write;algorithm=token_bucket',
      degraded: false,
    });
  });

  it('reports nulls rather than NaN when headers are absent', () => {
    const info = readRateLimit(new Headers());
    expect(info.limit).toBeNull();
    expect(info.remaining).toBeNull();
    expect(info.retryAfterSeconds).toBeNull();
  });

  it('flags a degraded limiter', () => {
    expect(readRateLimit(new Headers({ 'X-RateLimit-Degraded': 'true' })).degraded).toBe(true);
  });
});

describe('request', () => {
  it('returns parsed data plus rate-limit state on success', async () => {
    const payload = makeShortUrl();
    stubFetch(
      fakeResponse({
        status: 201,
        body: payload,
        headers: { 'X-RateLimit-Remaining': '19' },
      }),
    );

    const result = await request<typeof payload>('/api/v1/urls', {
      method: 'POST',
      body: { url: 'https://example.com' },
    });

    expect(result.data).toEqual(payload);
    expect(result.rateLimit.remaining).toBe(19);
  });

  it('sends a JSON content type only when there is a body', async () => {
    const fetchMock = stubFetch(fakeResponse({ body: makeShortUrl() }));
    await request('/api/v1/urls/aB3xY7z');
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ method: 'GET', headers: undefined });
  });

  it('resolves 204 responses without trying to parse a body', async () => {
    stubFetch(fakeResponse({ status: 204 }));
    await expect(
      request<void>('/api/v1/urls/aB3xY7z', { method: 'DELETE' }),
    ).resolves.toMatchObject({ data: undefined });
  });

  it('throws a structured ApiError for a 422 with field errors', async () => {
    stubFetch(
      fakeResponse({
        status: 422,
        body: errorBody('invalid_url', 'The provided URL is not valid.', {
          fields: [{ field: 'url', message: 'URL must include a host.' }],
          requestId: 'req-123',
        }),
      }),
    );

    const error = await request('/api/v1/urls', { method: 'POST', body: {} }).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.code).toBe('invalid_url');
    expect(apiError.fieldErrors).toHaveLength(1);
    expect(apiError.requestId).toBe('req-123');
  });

  it('surfaces a 429 as a rate-limit error carrying Retry-After', async () => {
    stubFetch(
      fakeResponse({
        status: 429,
        body: errorBody('rate_limited', 'Rate limit exceeded. Please slow down and retry shortly.'),
        headers: { 'Retry-After': '12', 'X-RateLimit-Remaining': '0' },
      }),
    );

    const error = (await request('/api/v1/urls', { method: 'POST', body: {} }).catch(
      (caught: unknown) => caught,
    )) as ApiError;

    expect(error.isRateLimited).toBe(true);
    expect(error.rateLimit?.retryAfterSeconds).toBe(12);
    expect(error.userMessage).toContain('Try again in 12 seconds');
  });

  it('classifies a failed fetch as a network error', async () => {
    stubFetch(new TypeError('Failed to fetch'));

    const error = (await request('/api/v1/urls').catch((caught: unknown) => caught)) as ApiError;

    expect(error.kind).toBe('network');
    expect(error.isDegraded).toBe(true);
    expect(error.userMessage).toMatch(/could not reach the server/i);
  });

  it('classifies a non-JSON error body (e.g. an Nginx 502 page) as a parse error', async () => {
    stubFetch(fakeResponse({ status: 502 }));

    const error = (await request('/api/v1/urls').catch((caught: unknown) => caught)) as ApiError;

    expect(error.kind).toBe('parse');
    expect(error.status).toBe(502);
  });

  it('treats a 503 as degraded so the UI can offer a retry', async () => {
    stubFetch(
      fakeResponse({
        status: 503,
        body: errorBody('dependency_unavailable', 'The database is currently unavailable.'),
      }),
    );

    const error = (await request('/api/v1/urls').catch((caught: unknown) => caught)) as ApiError;

    expect(error.isDegraded).toBe(true);
    expect(error.code).toBe('dependency_unavailable');
  });
});
