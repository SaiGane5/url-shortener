import { Link, NavLink, Outlet } from 'react-router-dom';
import { API_DOCS_URL } from '@/api/client';

/**
 * App shell: skip link, header nav, main landmark, footer.
 *
 * The skip link is the first focusable element on the page so a keyboard user
 * can jump past the navigation instead of tabbing through it on every route.
 */
export function Layout() {
  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="app__header">
        <div className="app__header-inner">
          <Link className="brand" to="/">
            <span className="brand__mark" aria-hidden="true">
              ↗
            </span>
            <span className="brand__text">
              short<strong>.link</strong>
            </span>
          </Link>

          <nav className="nav" aria-label="Main">
            <NavLink className="nav__link" to="/" end>
              Shorten
            </NavLink>
            {/* Points at the FastAPI-generated OpenAPI UI. Same-origin by
                default so it works behind the load balancer unchanged. */}
            <a className="nav__link" href={API_DOCS_URL} target="_blank" rel="noopener noreferrer">
              API docs
              <span className="nav__external" aria-hidden="true">
                ↗
              </span>
            </a>
          </nav>
        </div>
      </header>

      <main className="app__main" id="main">
        <Outlet />
      </main>

      <footer className="app__footer">
        <p>
          FastAPI + PostgreSQL + Redis behind Nginx, with a Redis-backed distributed rate limiter.
        </p>
      </footer>
    </div>
  );
}
