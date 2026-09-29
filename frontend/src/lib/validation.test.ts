import { describe, expect, it } from 'vitest';
import {
  localDateTimeToIso,
  normalizeUrlInput,
  validateAlias,
  validateExpiry,
  validateUrl,
} from './validation';

describe('normalizeUrlInput', () => {
  it('upgrades a bare host to https rather than http', () => {
    expect(normalizeUrlInput('example.com')).toBe('https://example.com');
  });

  it('leaves an explicit scheme alone', () => {
    expect(normalizeUrlInput('http://example.com')).toBe('http://example.com');
    expect(normalizeUrlInput('  https://example.com/x  ')).toBe('https://example.com/x');
  });

  it('does not invent a scheme for empty input', () => {
    expect(normalizeUrlInput('   ')).toBe('');
  });
});

describe('validateUrl', () => {
  it('accepts ordinary http and https URLs', () => {
    expect(validateUrl('https://example.com/a/b?c=d')).toBeNull();
    expect(validateUrl('http://example.com')).toBeNull();
    expect(validateUrl('example.com/path')).toBeNull();
  });

  it('requires a value', () => {
    expect(validateUrl('')).toMatch(/enter a url/i);
  });

  it('rejects javascript: URLs', () => {
    expect(validateUrl('javascript:alert(1)')).toMatch(/only http and https/i);
  });

  it('rejects data: URLs', () => {
    expect(validateUrl('data:text/html,<script>alert(1)</script>')).toMatch(/only http and https/i);
  });

  it('rejects credentials embedded in the URL', () => {
    expect(validateUrl('https://user:pass@example.com')).toMatch(/username or password/i);
  });

  it('rejects a URL longer than the backend limit', () => {
    expect(validateUrl(`https://example.com/${'a'.repeat(2100)}`)).toMatch(/at most 2048/i);
  });

  it('rejects unparseable input', () => {
    expect(validateUrl('http://')).not.toBeNull();
  });
});

describe('validateAlias', () => {
  it('treats an empty alias as valid because the field is optional', () => {
    expect(validateAlias('')).toBeNull();
  });

  it('accepts the documented charset', () => {
    expect(validateAlias('my-link_2')).toBeNull();
  });

  it('rejects too-short and too-long aliases', () => {
    expect(validateAlias('ab')).toMatch(/at least 4/i);
    expect(validateAlias('a'.repeat(17))).toMatch(/at most 16/i);
  });

  it('rejects characters that are not URL-path safe', () => {
    expect(validateAlias('bad alias')).toMatch(/letters, digits/i);
    expect(validateAlias('bad/alias')).toMatch(/letters, digits/i);
  });
});

describe('validateExpiry', () => {
  it('treats an empty expiry as valid because the field is optional', () => {
    expect(validateExpiry('')).toBeNull();
  });

  it('rejects a past timestamp', () => {
    expect(validateExpiry('2000-01-01T00:00')).toMatch(/in the future/i);
  });

  it('accepts a future timestamp', () => {
    const future = new Date(Date.now() + 86_400_000).toISOString().slice(0, 16);
    expect(validateExpiry(future)).toBeNull();
  });

  it('rejects nonsense', () => {
    expect(validateExpiry('not-a-date')).toMatch(/valid date/i);
  });
});

describe('localDateTimeToIso', () => {
  it('converts a local datetime-local value into a UTC instant', () => {
    const iso = localDateTimeToIso('2030-06-01T12:30');
    expect(iso).not.toBeNull();
    expect(iso).toBe(new Date('2030-06-01T12:30').toISOString());
  });

  it('returns null for empty or invalid input', () => {
    expect(localDateTimeToIso('')).toBeNull();
    expect(localDateTimeToIso('nope')).toBeNull();
  });
});
