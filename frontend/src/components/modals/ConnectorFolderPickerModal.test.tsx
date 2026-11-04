import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('../../api', () => ({
  api: {
    listConnectorFolders: vi.fn(),
  },
}));
vi.mock('../shared/Icon', () => ({ Icon: ({ name }: { name: string }) => <span data-testid={`icon-${name}`} /> }));

import { api } from '../../api';
import { useConnectorsStore } from '../../stores/connectorsStore';
import { ConnectorFolderPickerModal } from './ConnectorFolderPickerModal';

const mock = (fn: unknown) => fn as ReturnType<typeof vi.fn>;
const box = { id: 'box', name: 'Box', icon: 'Box', color: '#00f', connected: true, docs: 0, supportsFolderSync: true };

describe('ConnectorFolderPickerModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useConnectorsStore.setState({
      folderPicker: { connector: box },
      syncFolder: vi.fn(),
      syncWholeAccount: vi.fn(),
      closeFolderPicker: vi.fn(),
    } as never);
  });

  it('renders nothing when no picker is open', () => {
    useConnectorsStore.setState({ folderPicker: null } as never);
    const { container } = render(<ConnectorFolderPickerModal />);
    expect(container.innerHTML).toBe('');
  });

  it('lists the root folders, drills into one, and imports the chosen folder', async () => {
    mock(api.listConnectorFolders)
      .mockResolvedValueOnce({ folders: [{ id: 'f1', name: 'Clients', path: '/Clients' }], next_page_token: null })
      .mockResolvedValueOnce({ folders: [{ id: 'f2', name: 'Marlow', path: '/Clients/Marlow' }], next_page_token: null });
    render(<ConnectorFolderPickerModal />);

    expect(await screen.findByText('Clients')).toBeInTheDocument();
    expect(api.listConnectorFolders).toHaveBeenCalledWith('box', undefined);

    fireEvent.click(screen.getByLabelText('Open Clients'));
    expect(await screen.findByText('Marlow')).toBeInTheDocument();
    expect(api.listConnectorFolders).toHaveBeenLastCalledWith('box', 'f1');

    // With nothing selected inside, the import target is the open folder itself.
    expect(screen.getByText('Import "Clients"')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Marlow'));
    fireEvent.click(screen.getByText('Import "Marlow"'));
    expect(useConnectorsStore.getState().syncFolder).toHaveBeenCalledWith(box, 'f2', 'Marlow');
  });

  it('keeps the whole-account import reachable, behind the store flow', async () => {
    mock(api.listConnectorFolders).mockResolvedValue({ folders: [], next_page_token: null });
    render(<ConnectorFolderPickerModal />);
    await waitFor(() => expect(screen.getByText('No subfolders here.')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /Select a folder, or open one/ })).toBeDisabled();
    fireEvent.click(screen.getByText(/Import the entire account instead/));
    expect(useConnectorsStore.getState().syncWholeAccount).toHaveBeenCalledWith(box);
  });

  it('shows a listing error', async () => {
    mock(api.listConnectorFolders).mockRejectedValue(new Error('Connector not connected'));
    render(<ConnectorFolderPickerModal />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Connector not connected');
  });
});
