import { ApiError } from './errors';
import type { ApiErrorBody, RateLimitInfo } from './types';

/**
 * The single HTTP client. Every network call in the app goes through
 * `request()`, which means error parsing, rate-limit header handling, request
 * ids, and timeouts exist in exactly one place.
 *
 * Configuration comes from Vite env vars, never from hard-coded hosts. An
 * empty base URL means same-origin, which is how the Docker build ships: the
 * browser talks to the Nginx that served the page.
 */

const RAW_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

/** Normalised API origin. Empty string means "same origin as this page". */
export const API_BASE_URL = RAW_BASE_URL.replace(/\/+$/, '');

/** Origin used to display short links. Falls back to the API origin, then to the page. */
export const SHORT_LINK_BASE_URL = (
  (import.meta.env.VITE_SHORT_LINK_BASE_URL ?? '') ||
  API_BASE_URL
).replace(/\/+$/, '');

export const API_DOCS_URL = import.meta.env.VITE_API_DOCS_URL ?? '/docs';

/** Requests are abandoned after this long so the UI can never hang forever. */
const DEFAULT_TIMEOUT_MS = 10_000;

function parseIntOrNull(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Extract rate-limit state from response headers (present on 2xx and 429). */
export function readRateLimit(headers: Headers): RateLimitInfo {
  return {
    limit: parseIntOrNull(headers.get('X-RateLimit-Limit')),
    remaining: parseIntOrNull(headers.get('X-RateLimit-Remaining')),
    resetSeconds: parseIntOrNull(headers.get('X-RateLimit-Reset')),
    retryAfterSeconds: parseIntOrNull(headers.get('Retry-After')),
    policy: headers.get('X-RateLimit-Policy'),
    degraded: headers.get('X-RateLimit-Degraded') === 'true',
  };
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = (value as { error?: unknown }).error;
  return (
    typeof candidate === 'object' &&
    candidate !== null &&
    typeof (candidate as { code?: unknown }).code === 'string' &&
    typeof (candidate as { message?: unknown }).message === 'string'
  );
}

async function toApiError(response: Response): Promise<ApiError> {
  const rateLimit = readRateLimit(response.headers);
  const requestId = response.headers.get('X-Request-ID');

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON error body (an Nginx 502 page, for example).
    return new ApiError({
      kind: 'parse',
      message: `The server returned an unexpected ${response.status} response.`,
      status: response.status,
      code: `http_${response.status}`,
      requestId,
      rateLimit,
    });
  }

  if (!isApiErrorBody(body)) {
    return new ApiError({
      kind: 'parse',
      message: `The server returned an unexpected ${response.status} response.`,
      status: response.status,
      code: `http_${response.status}`,
      requestId,
      rateLimit,
    });
  }

  return new ApiError({
    kind: 'http',
    message: body.error.message,
    status: response.status,
    code: body.error.code,
    fieldErrors: body.error.details?.fields ?? [],
    requestId: body.error.request_id ?? requestId,
    rateLimit,
  });
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export interface ApiResult<T> {
  data: T;
  rateLimit: RateLimitInfo;
}

/**
 * Perform an API request.
 *
 * @throws ApiError - always, on any failure. Callers never have to inspect
 *   `response.ok` or distinguish a thrown `TypeError` from an HTTP error.
 */
export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;

  // Two independent reasons to abandon a request: the caller's signal (the
  // component unmounted, or a newer submit superseded this one) and our own
  // timeout. They are merged with a plain AbortController rather than
  // `AbortSignal.any`, which is not available in every browser or in jsdom.
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const forwardAbort = () => controller.abort();
  signal?.addEventListener('abort', forwardAbort, { once: true });

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      signal: controller.signal,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    // A caller-initiated abort is not a failure -- rethrow it so hooks can
    // ignore it instead of rendering an error the user did not cause.
    if (signal?.aborted) throw cause;
    throw new ApiError({
      kind: 'network',
      message: timedOut ? 'The request timed out.' : 'Could not reach the server.',
      code: timedOut ? 'timeout' : 'network_error',
    });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forwardAbort);
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  const rateLimit = readRateLimit(response.headers);

  if (response.status === 204) {
    return { data: undefined as T, rateLimit };
  }

  try {
    return { data: (await response.json()) as T, rateLimit };
  } catch {
    throw new ApiError({
      kind: 'parse',
      message: 'The server returned a malformed response.',
      status: response.status,
      code: 'malformed_response',
      rateLimit,
    });
  }
}
