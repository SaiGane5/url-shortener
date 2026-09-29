/**
 * Client-side validation.
 *
 * This exists purely for fast feedback -- it is NOT a security control and it
 * is not the source of truth. The backend re-validates everything (scheme
 * allowlist, SSRF/private-address rules, length limits, alias charset,
 * reserved codes, expiry), and its rejection is what the UI displays. The
 * rules here are deliberately a permissive subset: anything this file accepts
 * may still be refused by the server, but nothing it rejects would have been
 * accepted, so the user never gets a confusing "the form said it was fine"
 * moment.
 */

const ALLOWED_SCHEMES = ['http:', 'https:'];

/** Matches the backend's `URL_MAX_LENGTH` default. */
export const URL_MAX_LENGTH = 2048;
export const ALIAS_MIN_LENGTH = 4;
export const ALIAS_MAX_LENGTH = 16;
const ALIAS_PATTERN = /^[A-Za-z0-9_-]+$/;

/**
 * Add `https://` when the user typed a bare host.
 *
 * People type `example.com`. Rejecting that is technically correct and
 * practically hostile, so we upgrade it -- to https, never http, so the
 * default is the secure one.
 */
export function normalizeUrlInput(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed === '') return '';
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(trimmed)) return trimmed;
  return `https://${trimmed}`;
}

export function validateUrl(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === '') return 'Enter a URL to shorten.';

  const candidate = normalizeUrlInput(trimmed);
  if (candidate.length > URL_MAX_LENGTH) {
    return `URL must be at most ${URL_MAX_LENGTH} characters.`;
  }

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return 'That does not look like a valid URL.';
  }

  if (!ALLOWED_SCHEMES.includes(parsed.protocol)) {
    // Catches javascript: and data: before a request is even made.
    return 'Only http and https URLs can be shortened.';
  }
  if (parsed.hostname === '') {
    return 'The URL needs a host, for example https://example.com.';
  }
  if (parsed.username !== '' || parsed.password !== '') {
    return 'URLs containing a username or password are not allowed.';
  }
  return null;
}

export function validateAlias(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null; // optional field
  if (trimmed.length < ALIAS_MIN_LENGTH) {
    return `Custom alias must be at least ${ALIAS_MIN_LENGTH} characters.`;
  }
  if (trimmed.length > ALIAS_MAX_LENGTH) {
    return `Custom alias must be at most ${ALIAS_MAX_LENGTH} characters.`;
  }
  if (!ALIAS_PATTERN.test(trimmed)) {
    return 'Custom alias may only contain letters, digits, hyphens, and underscores.';
  }
  return null;
}

export function validateExpiry(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null; // optional field

  const parsed = new Date(trimmed);
  if (Number.isNaN(parsed.getTime())) return 'Enter a valid date and time.';
  if (parsed.getTime() <= Date.now()) return 'The expiry must be in the future.';
  return null;
}

/**
 * Convert the value of a `datetime-local` input into an ISO-8601 instant.
 *
 * `datetime-local` yields a wall-clock string with no zone. `new Date(...)`
 * interprets it in the browser's zone, which is what the user meant, and
 * `toISOString()` then sends an unambiguous UTC instant to the server.
 */
export function localDateTimeToIso(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const parsed = new Date(trimmed);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}
