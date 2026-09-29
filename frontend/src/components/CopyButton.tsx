import { useCopyToClipboard } from '@/hooks/useCopyToClipboard';

interface CopyButtonProps {
  value: string;
  /** Used in the accessible name, e.g. "Copy short URL". */
  label?: string;
}

/**
 * Copy-to-clipboard button.
 *
 * The result is announced through a live region rather than only by swapping
 * the label, so a screen reader user learns that the copy succeeded. The
 * button keeps a stable accessible name (`aria-label`) so its identity does
 * not change under the user mid-interaction.
 */
export function CopyButton({ value, label = 'short URL' }: CopyButtonProps) {
  const { state, copy } = useCopyToClipboard();

  const message =
    state === 'copied'
      ? 'Copied to clipboard'
      : state === 'failed'
        ? 'Copy failed. Select the link and copy manually.'
        : '';

  return (
    <>
      <button
        type="button"
        className="button button--secondary copy-button"
        onClick={() => void copy(value)}
        aria-label={`Copy ${label} to clipboard`}
      >
        <span aria-hidden="true">{state === 'copied' ? '✓' : '⧉'}</span>
        {state === 'copied' ? 'Copied' : 'Copy'}
      </button>
      <span role="status" aria-live="polite" className="visually-hidden">
        {message}
      </span>
      {state === 'failed' ? (
        <span className="copy-button__error" role="alert">
          Copy failed — select the link and copy manually.
        </span>
      ) : null}
    </>
  );
}
