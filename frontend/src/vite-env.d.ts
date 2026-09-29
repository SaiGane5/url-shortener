/// <reference types="vite/client" />

/**
 * Typed build-time configuration.
 *
 * Declaring these makes a typo like `VITE_API_URL` a compile error instead of
 * a silent `undefined` that only shows up as a failed request in production.
 * All values are optional strings because Vite omits any variable that is not
 * set, and every one of them is PUBLIC -- see frontend/.env.example.
 */
interface ImportMetaEnv {
  /** API origin. Empty/unset means same-origin (the default behind Nginx). */
  readonly VITE_API_BASE_URL?: string;
  /** Origin used to render short links. Falls back to the API origin. */
  readonly VITE_SHORT_LINK_BASE_URL?: string;
  /** Href for the "API docs" nav link. */
  readonly VITE_API_DOCS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
