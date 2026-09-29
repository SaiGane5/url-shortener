import { Alert } from './Alert';
import type { ApiError } from '@/api/errors';

interface ApiErrorNoticeProps {
  error: ApiError;
  /** Optional retry affordance for transient failures. */
  onRetry?: () => void;
}

/**
 * Renders an API failure.
 *
 * Every failure mode gets copy that tells the user what to do next, which is
 * the difference between an error message and a dead end:
 *
 *  - 429  -> how long to wait, taken from the server's Retry-After
 *  - 503  -> the service is degraded, retrying will help
 *  - network -> the request never left, check the connection
 *  - 422  -> the field-level messages the server returned
 *
 * The request id is shown as a footnote so a user can quote it and an operator
 * can find the exact structured log line for that request.
 */
export function ApiErrorNotice({ error, onRetry }: ApiErrorNoticeProps) {
  const title = error.isRateLimited
    ? 'Too many requests'
    : error.isDegraded
      ? 'Service temporarily unavailable'
      : error.isConflict
        ? 'That alias is taken'
        : 'Request failed';

  return (
    <Alert
      variant={error.isRateLimited ? 'warning' : 'error'}
      title={title}
      footnote={error.requestId ? `Request ID: ${error.requestId}` : null}
    >
      <p>{error.userMessage}</p>

      {error.fieldErrors.length > 0 ? (
        <ul className="alert__fields">
          {error.fieldErrors.map((fieldError) => (
            <li key={`${fieldError.field}:${fieldError.message}`}>
              {fieldError.field ? <strong>{fieldError.field}: </strong> : null}
              {fieldError.message}
            </li>
          ))}
        </ul>
      ) : null}

      {error.isRateLimited && error.rateLimit?.policy ? (
        <p className="alert__meta">
          Policy in effect: <code>{error.rateLimit.policy}</code>
        </p>
      ) : null}

      {onRetry && error.isDegraded ? (
        <button type="button" className="button button--secondary" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </Alert>
  );
}
