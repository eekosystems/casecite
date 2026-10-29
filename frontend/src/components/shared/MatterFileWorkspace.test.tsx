import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../api', () => ({
  api: {
    listMatters: vi.fn().mockResolvedValue({ matters: [] }),
    listDocuments: vi.fn().mockResolvedValue({ documents: [], total: 0 }),
  },
}));
vi.mock('../../utils/logger', () => ({
  default: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

import { MatterFileWorkspace } from './MatterFileWorkspace';
import { useMattersStore } from '../../stores/mattersStore';
import { useRagDocsStore } from '../../stores/ragDocsStore';
import type { Matter } from '../../api/types';
import type { DocumentFilter, RagDocument } from '../../types';

const matter = (id: string, name: string, extra: Partial<Matter> = {}): Matter => ({
  id,
  name,
  client_name: null,
  is_personal: false,
  owner_id: 'me',
  role: 'owner',
  member_count: 1,
  created_at: '2026-01-01T00:00:00Z',
  ...extra,
});

const doc = (id: string, matterId: string | null, extra: Partial<RagDocument> = {}): RagDocument => ({
  id,
  filename: `${id}.pdf`,
  content_type: 'application/pdf',
  size: 2048,
  source: 'local',
  status: 'indexed',
  chunk_count: 3,
  created_at: '2026-01-01T00:00:00Z',
  user_id: 'me',
  matter_id: matterId,
  ...extra,
});

const MATTERS = [
  matter('personal', 'Personal', { is_personal: true }),
  matter('smith', 'Smith v. Jones', { client_name: 'Smith Corp' }),
  matter('acme', 'Acme acquisition'),
];
const DOCS = [
  doc('unfiled', null),
  doc('complaint', 'smith'),
  doc('answer', 'smith'),
  doc('apa', 'acme'),
  doc('draft', 'acme', { status: 'processing' }),
];

function setup(currentFilter: DocumentFilter | null = null, activeMatterId: string | null = null) {
  useMattersStore.setState({ matters: MATTERS, _initialized: true, mattersLoading: false, activeMatterId });
  useRagDocsStore.setState({ ragDocs: DOCS, _initialized: true, ragDocsLoading: false } as never);
  const onScopeChange = vi.fn();
  const onOpenFile = vi.fn();
  const view = render(
    <MatterFileWorkspace currentFilter={currentFilter} onScopeChange={onScopeChange} onOpenFile={onOpenFile} />,
  );
  return { onScopeChange, onOpenFile, ...view };
}

describe('MatterFileWorkspace', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('lists every matter with its indexed document count', () => {
    setup();
    expect(screen.getByText('All matters')).toBeTruthy();
    expect(screen.getByText('Smith v. Jones')).toBeTruthy();
    expect(screen.getByText('Smith Corp')).toBeTruthy();
    expect(screen.getByText('Acme acquisition')).toBeTruthy();
    expect(screen.getByText('Your unfiled documents')).toBeTruthy();
    expect(screen.getByText('Chatting with everything')).toBeTruthy();
    expect(screen.getAllByText('2 docs').length).toBe(1); // Smith; Acme counts only its indexed file
    expect(screen.getAllByText('1 doc').length).toBe(2);
  });

  it('clicking a matter chats with it and makes it the active matter', () => {
    const { onScopeChange } = setup();
    fireEvent.click(screen.getByText('Smith v. Jones'));
    expect(useMattersStore.getState().activeMatterId).toBe('smith');
    expect(onScopeChange).toHaveBeenLastCalledWith(null);
  });

  it('the active matter shows as the selection', () => {
    setup(null, 'smith');
    expect(screen.getByText('Chatting with this matter')).toBeTruthy();
    expect(screen.queryByText('Chatting with everything')).toBeNull();
  });

  it('ticking a second matter chats with both', () => {
    const { onScopeChange } = setup(null, 'smith');
    fireEvent.click(screen.getByRole('button', { name: 'Add Acme acquisition to selection' }));
    expect(onScopeChange).toHaveBeenLastCalledWith({
      matter_ids: ['smith', 'acme'],
      document_ids: null,
      folder_paths: null,
      include_subfolders: true,
    });
  });

  it('summarises a multi-matter selection and clears back to everything', () => {
    const { onScopeChange } = setup({ matter_ids: ['smith', 'acme'], include_subfolders: true });
    expect(screen.getByText('Chatting with 2 matters')).toBeTruthy();
    fireEvent.click(screen.getByText('Clear'));
    expect(onScopeChange).toHaveBeenLastCalledWith(null);
    expect(useMattersStore.getState().activeMatterId).toBeNull();
  });

  it('unticking down to one matter returns to that active matter', () => {
    const { onScopeChange } = setup({ matter_ids: ['smith', 'acme'], include_subfolders: true });
    fireEvent.click(screen.getByRole('button', { name: 'Remove Acme acquisition from selection' }));
    expect(useMattersStore.getState().activeMatterId).toBe('smith');
    expect(onScopeChange).toHaveBeenLastCalledWith(null);
  });

  it('All matters resets the header and the selection', () => {
    const { onScopeChange } = setup(null, 'smith');
    fireEvent.click(screen.getByText('All matters'));
    expect(useMattersStore.getState().activeMatterId).toBeNull();
    expect(onScopeChange).toHaveBeenLastCalledWith(null);
  });

  it('a matter expands to its files, which can be added or opened', () => {
    const { onScopeChange, onOpenFile } = setup(null, 'smith');
    // The active matter opens on first load.
    expect(screen.getByText('complaint.pdf')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add complaint.pdf to selection' }));
    expect(onScopeChange).toHaveBeenLastCalledWith({
      matter_ids: ['smith'],
      document_ids: ['complaint'],
      folder_paths: null,
      include_subfolders: true,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Open answer.pdf' }));
    expect(onOpenFile).toHaveBeenCalledWith('answer', 'answer.pdf');
  });

  it('a file still indexing cannot be selected', () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Show files in Acme acquisition' }));
    expect(screen.getByText('indexing…')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Add draft.pdf to selection' })).toBeNull();
  });

  it('search narrows to matching matters and files', () => {
    setup();
    fireEvent.change(screen.getByLabelText('Search matters and files'), { target: { value: 'apa' } });
    expect(screen.getByText('apa.pdf')).toBeTruthy();
    expect(screen.queryByText('Smith v. Jones')).toBeNull();
  });
});
