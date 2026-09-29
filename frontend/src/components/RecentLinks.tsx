import { Link } from 'react-router-dom';
import { Spinner } from './Spinner';
import { formatRelative, truncateMiddle } from '@/lib/format';
import type { ShortUrl } from '@/api/types';

interface RecentLinksProps {
  items: ShortUrl[];
  isLoading: boolean;
  hasError: boolean;
  onRefresh: () => void;
}

/**
 * Recently created links.
 *
 * Has explicit loading, error, and empty states -- the empty state in
 * particular, because "no links yet" and "failed to load links" look identical
 * if you only render the list.
 */
export function RecentLinks({ items, isLoading, hasError, onRefresh }: RecentLinksProps) {
  return (
    <section className="recent" aria-labelledby="recent-heading">
      <div className="recent__header">
        <h2 id="recent-heading">Recent links</h2>
        <button
          type="button"
          className="button button--ghost button--small"
          onClick={onRefresh}
          disabled={isLoading}
        >
          Refresh
        </button>
      </div>

      {isLoading && items.length === 0 ? (
        <div className="recent__placeholder">
          <Spinner label="Loading recent links" />
        </div>
      ) : hasError ? (
        <p className="recent__placeholder recent__placeholder--error" role="status">
          Could not load recent links.
        </p>
      ) : items.length === 0 ? (
        <p className="recent__placeholder">
          No links yet. Shorten a URL above and it will appear here.
        </p>
      ) : (
        <ul className="recent__list">
          {items.map((item) => (
            <li key={item.short_code} className="recent__item">
              <div className="recent__item-main">
                <Link className="recent__code" to={`/urls/${item.short_code}`}>
                  /{item.short_code}
                </Link>
                <span className="recent__destination" title={item.original_url}>
                  {truncateMiddle(item.original_url, 48)}
                </span>
              </div>
              <div className="recent__item-side">
                <span className={`badge badge--${item.status}`}>{item.status}</span>
                <span className="recent__age">{formatRelative(item.created_at)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
