import type { FieldError, RateLimitInfo } from './types';

/**
 * One error type for every failure the UI can encounter, so components branch
 * on structured fields instead of parsing message strings.
 *
 * `kind` separates the three cases that need genuinely different UI:
 *  - `http`    the server answered with a structured error (has a code)
 *  - `network` the request never completed (offline, backend down, CORS)
 *  - `parse`   the server answered with something that was not our envelope
 */
export type ApiErrorKind = 'http' | 'network' | 'parse';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly code: string;
  readonly fieldErrors: FieldError[];
  readonly requestId: string | null;
  readonly rateLimit: RateLimitInfo | null;

  constructor(params: {
    kind: ApiErrorKind;
    message: string;
    status?: number | null;
    code?: string;
    fieldErrors?: FieldError[];
    requestId?: string | null;
    rateLimit?: RateLimitInfo | null;
  }) {
    super(params.message);
    this.name = 'ApiError';
    this.kind = params.kind;
    this.status = params.status ?? null;
    this.code = params.code ?? 'unknown_error';
    this.fieldErrors = params.fieldErrors ?? [];
    this.requestId = params.requestId ?? null;
    this.rateLimit = params.rateLimit ?? null;
  }

  get isRateLimited(): boolean {
    return this.status === 429 || this.code === 'rate_limited';
  }

  get isNotFound(): boolean {
    return this.status === 404 || this.code === 'not_found';
  }

  get isExpired(): boolean {
    return this.status === 410 || this.code === 'expired';
  }

  get isConflict(): boolean {
    return this.status === 409 || this.code === 'conflict';
  }

  /**
   * True when the backend is up but a dependency it needs is not (503), or the
   * request never reached it at all. These are the cases where "try again in a
   * moment" is genuinely the right advice.
   */
  get isDegraded(): boolean {
    return this.kind === 'network' || this.status === 503;
  }

  /**
   * Message to render to the user.
   *
   * The backend's message is preferred -- it is written for humans and knows
   * *why* something was rejected. Only the two cases the backend cannot
   * describe (no response at all, unparseable response) get client-side copy.
   */
  get userMessage(): string {
    if (this.kind === 'network') {
      return 'Could not reach the server. Check your connection and try again.';
    }
    if (this.isRateLimited) {
      const retry = this.rateLimit?.retryAfterSeconds;
      const suffix =
        retry && retry > 0 ? ` Try again in ${retry} second${retry === 1 ? '' : 's'}.` : '';
      return `${this.message}${suffix}`;
    }
    return this.message;
  }
}
