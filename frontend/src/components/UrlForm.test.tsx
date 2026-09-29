import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { UrlForm } from './UrlForm';

function renderForm(overrides: { isSubmitting?: boolean } = {}) {
  const onSubmit = vi.fn();
  render(<UrlForm onSubmit={onSubmit} isSubmitting={overrides.isSubmitting ?? false} />);
  return { onSubmit, user: userEvent.setup() };
}

describe('UrlForm', () => {
  it('renders an accessible, labelled URL field and submit button', () => {
    renderForm();
    expect(screen.getByLabelText(/long url/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /shorten/i })).toBeEnabled();
  });

  it('keeps the advanced options collapsed and out of view initially', () => {
    renderForm();
    const toggle = screen.getByRole('button', { name: /options/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByLabelText(/custom alias/i)).not.toBeVisible();
  });

  it('reveals expiry and alias inputs when the disclosure is opened', async () => {
    const { user } = renderForm();
    await user.click(screen.getByRole('button', { name: /options/i }));

    expect(screen.getByRole('button', { name: /options/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByLabelText(/custom alias/i)).toBeVisible();
    expect(screen.getByLabelText(/expires at/i)).toBeVisible();
  });

  it('blocks submission and shows a message when the URL is empty', async () => {
    const { onSubmit, user } = renderForm();
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/enter a url to shorten/i)).toBeInTheDocument();
  });

  it('marks the field invalid and links the error for assistive tech', async () => {
    const { user } = renderForm();
    const input = screen.getByLabelText(/long url/i);

    await user.type(input, 'javascript:alert(1)');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(input).toHaveAttribute('aria-invalid', 'true');
    const errorId = input.getAttribute('aria-describedby');
    expect(errorId).toBeTruthy();
    expect(document.getElementById(errorId!)).toHaveTextContent(/only http and https/i);
  });

  it('moves focus to the offending field on a failed submit', async () => {
    const { user } = renderForm();
    await user.click(screen.getByRole('button', { name: /shorten/i }));
    expect(screen.getByLabelText(/long url/i)).toHaveFocus();
  });

  it('normalises a bare host to https before submitting', async () => {
    const { onSubmit, user } = renderForm();

    await user.type(screen.getByLabelText(/long url/i), 'example.com/deep/path');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(onSubmit).toHaveBeenCalledWith({ url: 'https://example.com/deep/path' });
  });

  it('submits the optional alias and expiry when provided', async () => {
    const { onSubmit, user } = renderForm();

    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    await user.click(screen.getByRole('button', { name: /options/i }));
    await user.type(screen.getByLabelText(/custom alias/i), 'my-link');
    await user.type(screen.getByLabelText(/expires at/i), '2030-01-01T10:00');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const payload = onSubmit.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(payload.url).toBe('https://example.com');
    expect(payload.custom_alias).toBe('my-link');
    expect(typeof payload.expires_at).toBe('string');
  });

  it('rejects an invalid alias without calling the API', async () => {
    const { onSubmit, user } = renderForm();

    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    await user.click(screen.getByRole('button', { name: /options/i }));
    await user.type(screen.getByLabelText(/custom alias/i), 'ab');
    await user.click(screen.getByRole('button', { name: /shorten/i }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/at least 4 characters/i)).toBeInTheDocument();
  });

  it('clears a field error as soon as the user starts fixing it', async () => {
    const { user } = renderForm();

    await user.click(screen.getByRole('button', { name: /shorten/i }));
    expect(screen.getByText(/enter a url to shorten/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/long url/i), 'https://example.com');
    expect(screen.queryByText(/enter a url to shorten/i)).not.toBeInTheDocument();
  });

  it('shows a loading state and blocks resubmission while in flight', () => {
    renderForm({ isSubmitting: true });
    const submit = screen.getByRole('button', { name: /shortening/i });
    expect(submit).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent(/creating short link/i);
  });

  it('is fully operable with the keyboard alone', async () => {
    const { onSubmit, user } = renderForm();

    await user.tab();
    expect(screen.getByLabelText(/long url/i)).toHaveFocus();
    await user.keyboard('https://example.com{Enter}');

    expect(onSubmit).toHaveBeenCalledWith({ url: 'https://example.com' });
  });
});
