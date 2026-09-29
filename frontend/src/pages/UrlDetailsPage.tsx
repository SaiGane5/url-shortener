import { Link, useParams } from 'react-router-dom';
import { CopyButton } from '@/components/CopyButton';
import { Spinner } from '@/components/Spinner';
import { Alert } from '@/components/Alert';
import { ApiErrorNotice } from '@/components/ApiErrorNotice';
import { useUrlDetails } from '@/hooks/useUrlDetails';
import { formatDateTime, formatNumber, formatRelative, safeHostname } from '@/lib/format';

/**
 * `/urls/:code` -- metadata for one short link.
 *
 * Note the two distinct not-found-ish states: a 404 means the code never
 * existed or was deleted, while `status: "expired"` on a 200 means the link is
 * real but no longer redirects. Collapsing them would leave a user unable to
 * tell "I typo'd the code" from "this link ran out".
 */
export function UrlDetailsPage() {
  const { code } = useParams<{ code: string }>();
  const { status, data, error, refetch } = useUrlDetails(code);

  return (
    <div className="page page--details">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to="/">← Back to shortener</Link>
      </nav>

      <h1 className="page__title">
        Link details <code className="page__code">/{code}</code>
      </h1>

      {status === 'loading' ? (
        // One live region for the whole state: the visible text is the
        // announcement, and the spinner is purely decorative alongside it.
        <div className="card card--centered" role="status" aria-live="polite">
          <Spinner decorative />
          <p className="muted">Loading link details…</p>
        </div>
      ) : null}

      {status === 'error' && error ? (
        <div className="card">
          {error.isNotFound ? (
            <Alert variant="error" title="Link not found">
              <p>
                No link exists for the code <code>{code}</code>. It may have been deleted, or the
                code may be mistyped.
              </p>
              <Link className="button button--secondary" to="/">
                Create a new link
              </Link>
            </Alert>
          ) : (
            <ApiErrorNotice error={error} onRetry={refetch} />
          )}
        </div>
      ) : null}

      {status === 'success' && data ? (
        <>
          {data.status === 'expired' ? (
            <Alert variant="warning" title="This link has expired">
              Visitors now receive HTTP 410 Gone instead of a redirect. The metadata below is
              retained for reference.
            </Alert>
          ) : null}

          <section className="card" aria-labelledby="details-heading">
            <h2 className="visually-hidden" id="details-heading">
              Metadata
            </h2>

            <div className="details__link-row">
              <a
                className="result__short-url"
                href={data.short_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {data.short_url}
              </a>
              <CopyButton value={data.short_url} label="short URL" />
            </div>

            <dl className="details__grid">
              <div>
                <dt>Short code</dt>
                <dd>
                  <code>{data.short_code}</code>
                </dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>
                  <span className={`badge badge--${data.status}`}>{data.status}</span>
                </dd>
              </div>
              <div className="details__grid-wide">
                <dt>Original URL</dt>
                <dd>
                  {/* Rendered as text inside an anchor -- never as HTML. */}
                  <a
                    className="details__destination"
                    href={data.original_url}
                    target="_blank"
                    rel="noopener noreferrer nofollow"
                  >
                    {data.original_url}
                  </a>
                  {safeHostname(data.original_url) ? (
                    <span className="muted"> ({safeHostname(data.original_url)})</span>
                  ) : null}
                </dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>
                  {formatDateTime(data.created_at)}
                  <span className="muted"> ({formatRelative(data.created_at)})</span>
                </dd>
              </div>
              <div>
                <dt>Expires</dt>
                <dd>
                  {data.expires_at ? (
                    <>
                      {formatDateTime(data.expires_at)}
                      <span className="muted"> ({formatRelative(data.expires_at)})</span>
                    </>
                  ) : (
                    'Never'
                  )}
                </dd>
              </div>
              <div>
                <dt>Clicks</dt>
                <dd className="details__clicks">{formatNumber(data.click_count)}</dd>
              </div>
              <div>
                <dt>Last updated</dt>
                <dd>{formatDateTime(data.updated_at)}</dd>
              </div>
            </dl>

            <div className="details__actions">
              <button type="button" className="button button--secondary" onClick={refetch}>
                Refresh
              </button>
              <p className="muted details__note">
                Click counts are buffered in Redis and written back to PostgreSQL in batches; this
                page adds the un-flushed delta, so the number is accurate between flushes.
              </p>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}
