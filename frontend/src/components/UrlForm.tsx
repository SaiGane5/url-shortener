import { useId, useState } from 'react';
import type { FormEvent } from 'react';
import { Spinner } from './Spinner';
import {
  ALIAS_MAX_LENGTH,
  URL_MAX_LENGTH,
  localDateTimeToIso,
  normalizeUrlInput,
  validateAlias,
  validateExpiry,
  validateUrl,
} from '@/lib/validation';
import type { CreateUrlInput } from '@/api/types';

interface UrlFormProps {
  onSubmit: (input: CreateUrlInput) => void;
  isSubmitting: boolean;
  /** Cleared by the parent after a successful creation. */
  onDirty?: () => void;
}

interface FieldErrors {
  url?: string;
  alias?: string;
  expiry?: string;
}

/**
 * The URL creation form.
 *
 * Accessibility choices that are load-bearing rather than decorative:
 *  - every control has a real `<label htmlFor>`, not a placeholder-as-label
 *  - errors are wired with `aria-describedby` + `aria-invalid`, so a screen
 *    reader reads the message when focus lands on the offending field
 *  - validation runs on submit and on blur, never on every keystroke, so a
 *    half-typed URL is not screamed at
 *  - the submit button is never disabled on validation grounds: disabling it
 *    hides *why* nothing happened. It is disabled only while a request is
 *    genuinely in flight.
 */
export function UrlForm({ onSubmit, isSubmitting, onDirty }: UrlFormProps) {
  const urlId = useId();
  const aliasId = useId();
  const expiryId = useId();

  const [url, setUrl] = useState('');
  const [alias, setAlias] = useState('');
  const [expiry, setExpiry] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});

  function validateAll(): FieldErrors {
    const next: FieldErrors = {};
    const urlError = validateUrl(url);
    if (urlError) next.url = urlError;
    const aliasError = validateAlias(alias);
    if (aliasError) next.alias = aliasError;
    const expiryError = validateExpiry(expiry);
    if (expiryError) next.expiry = expiryError;
    return next;
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationErrors = validateAll();
    setErrors(validationErrors);
    if (Object.keys(validationErrors).length > 0) {
      // Move focus to the first invalid control so keyboard users are not left
      // guessing where the problem is.
      const firstInvalid = validationErrors.url
        ? urlId
        : validationErrors.alias
          ? aliasId
          : expiryId;
      document.getElementById(firstInvalid)?.focus();
      return;
    }

    const payload: CreateUrlInput = { url: normalizeUrlInput(url) };
    const isoExpiry = localDateTimeToIso(expiry);
    if (isoExpiry) payload.expires_at = isoExpiry;
    const trimmedAlias = alias.trim();
    if (trimmedAlias) payload.custom_alias = trimmedAlias;

    onSubmit(payload);
  }

  return (
    <form className="url-form" onSubmit={handleSubmit} noValidate>
      <div className="field">
        <label className="field__label" htmlFor={urlId}>
          Long URL
        </label>
        <div className="field__row">
          <input
            id={urlId}
            name="url"
            type="url"
            className={`input${errors.url ? ' input--invalid' : ''}`}
            placeholder="https://example.com/a/very/long/path"
            value={url}
            maxLength={URL_MAX_LENGTH}
            autoComplete="url"
            inputMode="url"
            required
            aria-invalid={errors.url ? true : undefined}
            aria-describedby={errors.url ? `${urlId}-error` : `${urlId}-hint`}
            onChange={(event) => {
              setUrl(event.target.value);
              if (errors.url) setErrors((previous) => ({ ...previous, url: undefined }));
              onDirty?.();
            }}
            onBlur={() => {
              const error = validateUrl(url);
              if (url.trim() !== '') {
                setErrors((previous) => ({ ...previous, url: error ?? undefined }));
              }
            }}
          />
          <button type="submit" className="button button--primary" disabled={isSubmitting}>
            {isSubmitting ? <Spinner size="sm" label="Creating short link" /> : null}
            {isSubmitting ? 'Shortening…' : 'Shorten'}
          </button>
        </div>
        {errors.url ? (
          <p className="field__error" id={`${urlId}-error`}>
            {errors.url}
          </p>
        ) : (
          <p className="field__hint" id={`${urlId}-hint`}>
            http and https only. A bare host like <code>example.com</code> is upgraded to https.
          </p>
        )}
      </div>

      <button
        type="button"
        className="disclosure"
        aria-expanded={showAdvanced}
        aria-controls="advanced-options"
        onClick={() => setShowAdvanced((open) => !open)}
      >
        <span aria-hidden="true">{showAdvanced ? '▾' : '▸'}</span>
        Options (expiry, custom alias)
      </button>

      {/* `hidden` rather than conditional rendering: keeps the ids stable and
          keeps the panel out of the tab order when collapsed. */}
      <div className="advanced" id="advanced-options" hidden={!showAdvanced}>
        <div className="field">
          <label className="field__label" htmlFor={expiryId}>
            Expires at <span className="field__optional">(optional)</span>
          </label>
          <input
            id={expiryId}
            name="expires_at"
            type="datetime-local"
            className={`input${errors.expiry ? ' input--invalid' : ''}`}
            value={expiry}
            aria-invalid={errors.expiry ? true : undefined}
            aria-describedby={errors.expiry ? `${expiryId}-error` : `${expiryId}-hint`}
            onChange={(event) => {
              setExpiry(event.target.value);
              if (errors.expiry) setErrors((previous) => ({ ...previous, expiry: undefined }));
            }}
            onBlur={() => {
              const error = validateExpiry(expiry);
              setErrors((previous) => ({ ...previous, expiry: error ?? undefined }));
            }}
          />
          {errors.expiry ? (
            <p className="field__error" id={`${expiryId}-error`}>
              {errors.expiry}
            </p>
          ) : (
            <p className="field__hint" id={`${expiryId}-hint`}>
              Interpreted in your local timezone and sent to the server as UTC. Leave empty for a
              link that never expires.
            </p>
          )}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={aliasId}>
            Custom alias <span className="field__optional">(optional)</span>
          </label>
          <input
            id={aliasId}
            name="custom_alias"
            type="text"
            className={`input${errors.alias ? ' input--invalid' : ''}`}
            placeholder="my-link"
            value={alias}
            maxLength={ALIAS_MAX_LENGTH}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={errors.alias ? true : undefined}
            aria-describedby={errors.alias ? `${aliasId}-error` : `${aliasId}-hint`}
            onChange={(event) => {
              setAlias(event.target.value);
              if (errors.alias) setErrors((previous) => ({ ...previous, alias: undefined }));
            }}
            onBlur={() => {
              const error = validateAlias(alias);
              setErrors((previous) => ({ ...previous, alias: error ?? undefined }));
            }}
          />
          {errors.alias ? (
            <p className="field__error" id={`${aliasId}-error`}>
              {errors.alias}
            </p>
          ) : (
            <p className="field__hint" id={`${aliasId}-hint`}>
              4–{ALIAS_MAX_LENGTH} characters: letters, digits, hyphens, underscores. Leave empty
              for a random code.
            </p>
          )}
        </div>
      </div>
    </form>
  );
}
