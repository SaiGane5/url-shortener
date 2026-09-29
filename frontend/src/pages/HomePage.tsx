import { useCallback, useEffect, useRef } from 'react';
import { UrlForm } from '@/components/UrlForm';
import { ShortUrlResult } from '@/components/ShortUrlResult';
import { ApiErrorNotice } from '@/components/ApiErrorNotice';
import { RecentLinks } from '@/components/RecentLinks';
import { Alert } from '@/components/Alert';
import { useCreateUrl } from '@/hooks/useCreateUrl';
import { useRecentUrls } from '@/hooks/useRecentUrls';
import type { CreateUrlInput } from '@/api/types';

/** Remaining-request threshold at which we warn the user before they hit 429. */
const LOW_BUDGET_THRESHOLD = 5;

export function HomePage() {
  const { status, result, error, rateLimit, submit, reset } = useCreateUrl();
  const recent = useRecentUrls(5);

  // Remembered so a transient failure (backend degraded, request never
  // delivered) can be retried without making the user retype the URL.
  const lastInput = useRef<CreateUrlInput | null>(null);

  const handleSubmit = useCallback(
    (input: CreateUrlInput) => {
      lastInput.current = input;
      void submit(input);
    },
    [submit],
  );

  const handleRetry = useCallback(() => {
    if (lastInput.current) void submit(lastInput.current);
  }, [submit]);

  // A new link should show up in the recent list without a manual refresh.
  useEffect(() => {
    if (status === 'success') recent.refresh();
    // `recent.refresh` is stable (useCallback with no deps); depending on the
    // whole `recent` object would refetch on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const showLowBudgetWarning =
    status !== 'error' &&
    rateLimit?.remaining !== null &&
    rateLimit?.remaining !== undefined &&
    rateLimit.remaining <= LOW_BUDGET_THRESHOLD;

  return (
    <div className="page page--home">
      <section className="hero">
        <h1 className="hero__title">Shorten a URL</h1>
        <p className="hero__subtitle">
          Links are resolved from a Redis cache with a PostgreSQL fallback, and every request passes
          through a distributed rate limiter shared by all API replicas.
        </p>
      </section>

      <section className="card" aria-labelledby="create-heading">
        <h2 className="visually-hidden" id="create-heading">
          Create a short link
        </h2>

        <UrlForm onSubmit={handleSubmit} isSubmitting={status === 'loading'} />

        {error ? (
          <div className="card__notice">
            <ApiErrorNotice error={error} onRetry={handleRetry} />
          </div>
        ) : null}

        {showLowBudgetWarning ? (
          <div className="card__notice">
            <Alert variant="info" title="Approaching the rate limit">
              {rateLimit.remaining} request{rateLimit.remaining === 1 ? '' : 's'} left in this
              window
              {rateLimit.resetSeconds ? `; the budget refills in ${rateLimit.resetSeconds}s` : ''}.
            </Alert>
          </div>
        ) : null}

        {status === 'success' && result ? (
          <div className="card__result">
            <ShortUrlResult result={result} onCreateAnother={reset} />
          </div>
        ) : null}
      </section>

      <RecentLinks
        items={recent.items}
        isLoading={recent.status === 'loading'}
        hasError={recent.status === 'error'}
        onRefresh={recent.refresh}
      />
    </div>
  );
}
