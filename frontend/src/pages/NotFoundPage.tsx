import { Link } from 'react-router-dom';

export function NotFoundPage() {
  return (
    <div className="page page--centered">
      <div className="card card--centered">
        <h1 className="page__title">Page not found</h1>
        <p className="muted">
          This route does not exist in the app. If you were following a short link, the code may
          have been deleted or mistyped.
        </p>
        <Link className="button button--primary" to="/">
          Go to the shortener
        </Link>
      </div>
    </div>
  );
}
