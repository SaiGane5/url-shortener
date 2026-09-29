import { Link } from 'react-router-dom';
import { CopyButton } from './CopyButton';
import { formatDateTime, safeHostname, truncateMiddle } from '@/lib/format';
import type { ShortUrl } from '@/api/types';

interface ShortUrlResultProps {
  result: ShortUrl;
  onCreateAnother: () => void;
}

/**
 * Success panel shown after a link is created.
 *
 * Security note: the destination is rendered as text, and the only element
 * that navigates to it is an `<a href>` whose value came back from the API
 * already validated and normalised. There is no `dangerouslySetInnerHTML`
 * anywhere in this app, so a destination containing markup is displayed
 * literally rather than executed. `rel="noopener noreferrer"` keeps the
 * destination from reaching back through `window.opener` or learning where
 * the click came from.
 */
export function ShortUrlResult({ result, onCreateAnother }: ShortUrlResultProps) {
  const host = safeHostname(result.original_url);

  return (
    <section className="result" aria-labelledby="result-heading">
      {/* Polite live region: announced after the form's own updates settle. */}
      <p className="visually-hidden" role="status" aria-live="polite">
        Short link created: {result.short_url}
      </p>

      <h2 className="result__heading" id="result-heading">
        Your short link is ready
      </h2>

      <div className="result__link-row">
        <a
          className="result__short-url"
          href={result.short_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {result.short_url}
        </a>
        <CopyButton value={result.short_url} label="short URL" />
      </div>

      <dl className="result__meta">
        <div>
          <dt>Destination</dt>
          <dd title={result.original_url}>
            {truncateMiddle(result.original_url)}
            {host ? <span className="result__host"> ({host})</span> : null}
          </dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{formatDateTime(result.created_at)}</dd>
        </div>
        <div>
          <dt>Expires</dt>
          <dd>{result.expires_at ? formatDateTime(result.expires_at) : 'Never'}</dd>
        </div>
        <div>
          <dt>Clicks</dt>
          <dd>{result.click_count}</dd>
        </div>
      </dl>

      <div className="result__actions">
        <Link className="button button--secondary" to={`/urls/${result.short_code}`}>
          View details
        </Link>
        <button type="button" className="button button--ghost" onClick={onCreateAnother}>
          Shorten another
        </button>
      </div>
    </section>
  );
}
