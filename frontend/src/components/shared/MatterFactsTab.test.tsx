import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MatterFactsTab } from './MatterFactsTab';

const styles = new Proxy({} as Record<string, string>, { get: (_, key) => String(key) });
const members = [
  { user_id: 'me', email: 'me@firm.com', name: 'Me', role: 'owner' },
  { user_id: 'col', email: 'col@firm.com', name: 'Colleague', role: 'member' },
];

function renderTab(overrides: Partial<Parameters<typeof MatterFactsTab>[0]> = {}) {
  const props = {
    facts: 'Lease signed 1 March 2024.',
    draft: null,
    updatedAt: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
    updatedBy: 'col',
    members,
    maxChars: 20_000,
    saving: false,
    readOnly: false,
    onChange: vi.fn(),
    onSave: vi.fn(),
    styles,
    ...overrides,
  };
  render(<MatterFactsTab {...props} />);
  return props;
}

describe('MatterFactsTab', () => {
  it('shows the saved facts, the count against the limit, and who edited them last', () => {
    renderTab();
    expect((screen.getByLabelText('Facts') as HTMLTextAreaElement).value).toBe('Lease signed 1 March 2024.');
    expect(screen.getByText('26 / 20,000 characters')).toBeTruthy();
    expect(screen.getByText(/Last edited 2 hours ago by Colleague/)).toBeTruthy();
  });

  it('explains how the facts are used', () => {
    renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'About the matter facts' }));
    expect(screen.getByRole('tooltip').textContent).toMatch(/every chat and strategy brief/i);
  });

  it('Save is disabled until there are unsaved edits', () => {
    const props = renderTab();
    const save = screen.getByRole('button', { name: /Save/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Facts'), { target: { value: 'Lease signed 2 March 2024.' } });
    expect(props.onChange).toHaveBeenCalledWith('Lease signed 2 March 2024.');
  });

  it('with a draft it shows the edits, flags them unsaved, and saves on click', () => {
    const props = renderTab({ draft: 'Edited facts' });
    expect((screen.getByLabelText('Facts') as HTMLTextAreaElement).value).toBe('Edited facts');
    expect(screen.getByText('Unsaved changes')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Save/ }));
    expect(props.onSave).toHaveBeenCalled();
  });

  it('read-only: says so, locks the text and offers no Save', () => {
    renderTab({ readOnly: true });
    expect(screen.getByText(/Your role can read these facts but not edit them/)).toBeTruthy();
    expect((screen.getByLabelText('Facts') as HTMLTextAreaElement).readOnly).toBe(true);
    expect(screen.queryByRole('button', { name: /Save/ })).toBeNull();
  });

  it('an unknown editor reads as "a member"', () => {
    renderTab({ updatedBy: 'someone-removed' });
    expect(screen.getByText(/by a member/)).toBeTruthy();
  });

  it('Save is disabled while saving', () => {
    renderTab({ draft: 'Edited', saving: true });
    expect((screen.getByRole('button', { name: /Saving/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});
