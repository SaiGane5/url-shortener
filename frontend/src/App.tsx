import { Route, Routes } from 'react-router-dom';
import { Layout } from '@/components/Layout';
import { HomePage } from '@/pages/HomePage';
import { UrlDetailsPage } from '@/pages/UrlDetailsPage';
import { NotFoundPage } from '@/pages/NotFoundPage';

/**
 * Route table.
 *
 * Note that no route is a bare `/:code`. Short-code resolution belongs to the
 * backend -- Nginx routes single-segment paths to the API, so a short link
 * never loads the SPA at all. Doing it here instead would mean loading a
 * JavaScript bundle just to issue a redirect, which is slower and breaks for
 * clients that do not execute JS.
 */
export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/urls/:code" element={<UrlDetailsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
