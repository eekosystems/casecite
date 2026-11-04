import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MatterMembersTab } from './MatterMembersTab';

vi.mock('./Icon', () => ({ Icon: ({ name }: { name: string }) => <span data-testid={`icon-${name}`} /> }));

const s = new Proxy({} as Record<string, string>, { get: (_, key) => String(key) });

const members = [
  { user_id: 'u-owner', email: 'owner@firm.com', name: 'Olive Owner', role: 'owner' },
  { user_id: 'u-2', email: 'sam@firm.com', name: 'Sam', role: 'member' },
];

function renderTab(overrides: Partial<React.ComponentProps<typeof MatterMembersTab>> = {}) {
  const props = {
    members,
    isOwner: true,
    isPersonal: false,
    currentUserId: 'u-owner',
    onAdd: vi.fn().mockResolvedValue(true),
    onRemove: vi.fn(),
    onTransfer: vi.fn(),
    styles: s,
    ...overrides,
  };
  render(<MatterMembersTab {...props} />);
  return props;
}

describe('MatterMembersTab ethical walls', () => {
  it('shows no wall controls to non-admins', () => {
    renderTab();
    expect(screen.queryByTestId('ethical-walls')).toBeNull();
  });

  it('lets an admin wall a user off with a reason', async () => {
    const onAddWall = vi.fn().mockResolvedValue(true);
    renderTab({ isAdmin: true, walls: [], onAddWall, onRemoveWall: vi.fn() });
    fireEvent.change(screen.getByLabelText('Email of the user to wall off'), { target: { value: 'conflict@firm.com' } });
    fireEvent.change(screen.getByLabelText('Wall reason'), { target: { value: 'Prior representation' } });
    fireEvent.click(screen.getByText('Wall off'));
    await waitFor(() => expect(onAddWall).toHaveBeenCalledWith('conflict@firm.com', 'Prior representation'));
  });

  it('lists walls and lifts one', async () => {
    const onRemoveWall = vi.fn().mockResolvedValue(true);
    renderTab({
      isAdmin: true,
      onAddWall: vi.fn(),
      onRemoveWall,
      walls: [{ user_id: 'u-9', email: 'c@firm.com', name: 'Casey', reason: 'Conflict', created_by: 'admin', created_at: '2026-11-01T00:00:00Z' }],
    });
    expect(screen.getByText('Casey')).toBeInTheDocument();
    expect(screen.getByText(/Conflict/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Lift wall'));
    await waitFor(() => expect(onRemoveWall).toHaveBeenCalledWith('u-9'));
  });
});
