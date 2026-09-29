import type { ReactNode } from 'react';

export type AlertVariant = 'error' | 'warning' | 'success' | 'info';

interface AlertProps {
  variant: AlertVariant;
  title?: string;
  children: ReactNode;
  /** Rendered small and muted -- used for the request id on failures. */
  footnote?: string | null;
}

const ICONS: Record<AlertVariant, string> = {
  error: '!',
  warning: '!',
  success: '✓',
  info: 'i',
};

/**
 * Status message.
 *
 * `role="alert"` (assertive) for errors and warnings so a screen reader
 * announces a failed submission immediately; `role="status"` (polite) for
 * success and info so a confirmation does not interrupt what the user is
 * doing.
 */
export function Alert({ variant, title, children, footnote }: AlertProps) {
  const assertive = variant === 'error' || variant === 'warning';
  return (
    <div
      className={`alert alert--${variant}`}
      role={assertive ? 'alert' : 'status'}
      aria-live={assertive ? 'assertive' : 'polite'}
    >
      <span className="alert__icon" aria-hidden="true">
        {ICONS[variant]}
      </span>
      <div className="alert__body">
        {title ? <p className="alert__title">{title}</p> : null}
        <div className="alert__message">{children}</div>
        {footnote ? <p className="alert__footnote">{footnote}</p> : null}
      </div>
    </div>
  );
}
