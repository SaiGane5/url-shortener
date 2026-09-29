import { useCallback, useEffect, useState } from 'react';
import { getShortUrl } from '@/api/urls';
import { ApiError } from '@/api/errors';
import type { ShortUrl } from '@/api/types';
import type { RequestStatus } from './useCreateUrl';

interface UrlDetailsState {
  status: RequestStatus;
  data: ShortUrl | null;
  error: ApiError | null;
}

/** Fetches metadata for a short code, refetching whenever the code changes. */
export function useUrlDetails(code: string | undefined) {
  const [state, setState] = useState<UrlDetailsState>({
    status: code ? 'loading' : 'idle',
    data: null,
    error: null,
  });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!code) {
      setState({ status: 'idle', data: null, error: null });
      return;
    }

    // Aborted on unmount and on code change, so a slow response for an old
    // code can never overwrite the state of the current one.
    const controller = new AbortController();
    setState({ status: 'loading', data: null, error: null });

    void (async () => {
      try {
        const { data } = await getShortUrl(code, controller.signal);
        if (!controller.signal.aborted) {
          setState({ status: 'success', data, error: null });
        }
      } catch (cause) {
        if (controller.signal.aborted) return;
        const error =
          cause instanceof ApiError
            ? cause
            : new ApiError({ kind: 'network', message: 'Something went wrong.' });
        setState({ status: 'error', data: null, error });
      }
    })();

    return () => controller.abort();
  }, [code, reloadToken]);

  const refetch = useCallback(() => setReloadToken((token) => token + 1), []);

  return { ...state, refetch };
}
