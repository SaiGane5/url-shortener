interface SpinnerProps {
  /** Announced to assistive tech; also the visually hidden label. */
  label?: string;
  size?: 'sm' | 'md';
  /**
   * Set when adjacent visible text already announces the loading state.
   * Without this, a spinner next to "Loading link details…" produces two
   * screen-reader announcements for one event.
   */
  decorative?: boolean;
}

/**
 * Loading indicator.
 *
 * The animation itself is always `aria-hidden` -- a spinning div means nothing
 * to a screen reader -- and the meaning is carried by text. The CSS honours
 * `prefers-reduced-motion` (see styles/index.css).
 */
export function Spinner({ label = 'Loading', size = 'md', decorative = false }: SpinnerProps) {
  if (decorative) {
    return (
      <span className={`spinner spinner--${size}`} aria-hidden="true">
        <span className="spinner__ring" />
      </span>
    );
  }
  return (
    <span className={`spinner spinner--${size}`} role="status">
      <span className="spinner__ring" aria-hidden="true" />
      <span className="visually-hidden">{label}</span>
    </span>
  );
}
