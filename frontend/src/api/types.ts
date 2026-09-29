/**
 * Types mirroring the backend's OpenAPI schemas.
 *
 * Hand-written rather than generated: the surface is four endpoints and one
 * error envelope, so a generator would add a build step and a large generated
 * file for very little. If the API grew, `openapi-typescript` against
 * /openapi.json would be the right move.
 */

export type UrlStatus = 'active' | 'expired';

export interface ShortUrl {
  short_code: string;
  short_url: string;
  original_url: string;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
  click_count: number;
  status: UrlStatus;
}

export interface UrlListPage {
  items: ShortUrl[];
  next_cursor: string | null;
}

export interface CreateUrlInput {
  url: string;
  expires_at?: string | null;
  custom_alias?: string | null;
}

/** Field-level validation failures returned by the backend (422). */
export interface FieldError {
  field: string;
  message: string;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: {
      fields?: FieldError[];
      reason?: string;
      policy?: string;
      dependency?: string;
    } | null;
    request_id?: string | null;
  };
}

/** Rate-limit state parsed from response headers. */
export interface RateLimitInfo {
  limit: number | null;
  remaining: number | null;
  resetSeconds: number | null;
  retryAfterSeconds: number | null;
  policy: string | null;
  /** True when the server answered without a working limiter. */
  degraded: boolean;
}
