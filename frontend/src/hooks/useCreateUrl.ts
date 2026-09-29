import { useCallback, useRef, useState } from 'react';
import { createShortUrl } from '@/api/urls';
import { ApiError } from '@/api/errors';
import type { CreateUrlInput, RateLimitInfo, ShortUrl } from '@/api/types';

export type RequestStatus = 'idle' | 'loading' | 'success' | 'error';

interface CreateUrlState {
  status: RequestStatus;
  result: ShortUrl | null;
  error: ApiError | null;
  rateLimit: RateLimitInfo | null;
}

const INITIAL_STATE: CreateUrlState = {
  status: 'idle',
  result: null,
  error: null,
  rateLimit: null,
};

/**
 * Owns the create-URL request lifecycle.
 *
 * State lives in a hook rather than in the form component so the form stays a
 * presentational concern, and so the loading/error/success states are modelled
 * as one discriminated status instead of three booleans that can contradict
 * each other (`isLoading && error` is not representable here).
 */
export function useCreateUrl() {
  const [state, setState] = useState<CreateUrlState>(INITIAL_STATE);
  const inFlight = useRef<AbortController | null>(null);

  const submit = useCallback(async (input: CreateUrlInput): Promise<ShortUrl | null> => {
    // Supersede an in-flight request: a double-clicked submit button must not
    // create two links, and the later response must win.
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    setState({ status: 'loading', result: null, error: null, rateLimit: null });

    try {
      const { data, rateLimit } = await createShortUrl(input, controller.signal);
      setState({ status: 'success', result: data, error: null, rateLimit });
      return data;
    } catch (cause) {
      if (controller.signal.aborted) return null; // superseded, not a failure
      const error =
        cause instanceof ApiError
          ? cause
          : new ApiError({ kind: 'network', message: 'Something went wrong.' });
      setState({ status: 'error', result: null, error, rateLimit: error.rateLimit });
      return null;
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    inFlight.current?.abort();
    inFlight.current = null;
    setState(INITIAL_STATE);
  }, []);

  return { ...state, submit, reset };
}
