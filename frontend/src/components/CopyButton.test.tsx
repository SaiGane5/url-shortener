import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CopyButton } from './CopyButton';

const VALUE = 'http://localhost:8080/aB3xY7z';

/**
 * Order matters here: `userEvent.setup()` installs its own `navigator.clipboard`
 * stub, so the clipboard must be replaced AFTER setup or user-event's version
 * silently wins and the component's real code path is never exercised.
 */
function renderCopyButton(clipboard: { writeText: ReturnType<typeof vi.fn> } | undefined) {
  const user = userEvent.setup();
  vi.stubGlobal('navigator', { ...navigator, clipboard });
  render(<CopyButton value={VALUE} />);
  return user;
}

function stubExecCommand(result: boolean) {
  const execCommand = vi.fn().mockReturnValue(result);
  Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true });
  return execCommand;
}

describe('CopyButton', () => {
  it('has an accessible name that survives the label change', () => {
    render(<CopyButton value={VALUE} />);
    expect(
      screen.getByRole('button', { name: /copy short url to clipboard/i }),
    ).toBeInTheDocument();
  });

  it('writes the value to the clipboard and confirms it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = renderCopyButton({ writeText });

    await user.click(screen.getByRole('button', { name: /copy short url/i }));

    expect(writeText).toHaveBeenCalledWith(VALUE);
    await waitFor(() => expect(screen.getByText('Copied')).toBeInTheDocument());
  });

  it('announces the result in a live region', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = renderCopyButton({ writeText });

    await user.click(screen.getByRole('button', { name: /copy short url/i }));

    await waitFor(() =>
      expect(
        screen
          .getAllByRole('status')
          .some((node) => /copied to clipboard/i.test(node.textContent ?? '')),
      ).toBe(true),
    );
  });

  it('falls back to execCommand when the Clipboard API is unavailable', async () => {
    const execCommand = stubExecCommand(true);
    const user = renderCopyButton(undefined);

    await user.click(screen.getByRole('button', { name: /copy short url/i }));

    expect(execCommand).toHaveBeenCalledWith('copy');
    await waitFor(() => expect(screen.getByText('Copied')).toBeInTheDocument());
  });

  it('tells the user to copy manually when every method fails', async () => {
    stubExecCommand(false);
    const writeText = vi.fn().mockRejectedValue(new Error('permission denied'));
    const user = renderCopyButton({ writeText });

    await user.click(screen.getByRole('button', { name: /copy short url/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/copy failed/i));
  });
});
