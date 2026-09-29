import { request } from './client';
import type { ApiResult } from './client';
import type { CreateUrlInput, ShortUrl, UrlListPage } from './types';

/**
 * Endpoint bindings. Thin on purpose: no business logic lives here, because
 * validation, expiry, and rate limiting are the backend's decisions and
 * duplicating them client-side would create two sources of truth that drift.
 */

export function createShortUrl(
  input: CreateUrlInput,
  signal?: AbortSignal,
): Promise<ApiResult<ShortUrl>> {
  return request<ShortUrl>('/api/v1/urls', { method: 'POST', body: input, signal });
}

export function getShortUrl(code: string, signal?: AbortSignal): Promise<ApiResult<ShortUrl>> {
  return request<ShortUrl>(`/api/v1/urls/${encodeURIComponent(code)}`, { signal });
}

export function deleteShortUrl(code: string, signal?: AbortSignal): Promise<ApiResult<void>> {
  return request<void>(`/api/v1/urls/${encodeURIComponent(code)}`, { method: 'DELETE', signal });
}

export function listShortUrls(
  params: { limit?: number; cursor?: string | null } = {},
  signal?: AbortSignal,
): Promise<ApiResult<UrlListPage>> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.cursor) query.set('cursor', params.cursor);
  const suffix = query.size > 0 ? `?${query.toString()}` : '';
  return request<UrlListPage>(`/api/v1/urls${suffix}`, { signal });
}
