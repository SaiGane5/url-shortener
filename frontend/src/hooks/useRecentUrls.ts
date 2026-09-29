import { useCallback, useEffect, useState } from 'react';
import { listShortUrls } from '@/api/urls';
import { ApiError } from '@/api/errors';
import type { ShortUrl } from '@/api/types';
import type { RequestStatus } from './useCreateUrl';

interface RecentUrlsState {
  status: RequestStatus;
  items: ShortUrl[];
  error: ApiError | null;
}

/**
 * Recently created links, for the home page panel.
 *
 * Failure here is non-fatal by design: the panel is supplementary, so a failed
 * listing shows a small inline notice rather than blocking the URL form, which
 * is the thing users actually came for.
 */
export function useRecentUrls(limit = 5) {
  const [state, setState] = useState<RecentUrlsState>({
    status: 'loading',
    items: [],
    error: null,
  });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState((previous) => ({ ...previous, status: 'loading' }));

    void (async () => {
      try {
        const { data } = await listShortUrls({ limit }, controller.signal);
        if (!controller.signal.aborted) {
          setState({ status: 'success', items: data.items, error: null });
        }
      } catch (cause) {
        if (controller.signal.aborted) return;
        const error =
          cause instanceof ApiError
            ? cause
            : new ApiError({ kind: 'network', message: 'Could not load recent links.' });
        setState({ status: 'error', items: [], error });
      }
    })();

    return () => controller.abort();
  }, [limit, reloadToken]);

  const refresh = useCallback(() => setReloadToken((token) => token + 1), []);

  return { ...state, refresh };
}
