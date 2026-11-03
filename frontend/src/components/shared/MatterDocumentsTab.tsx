import { useState } from 'react';
import { Icon } from './Icon';
import { formatFileSize } from '../../utils';
import type { RagDocument } from '../../types';

interface MatterDocumentsTabProps {
  documents: RagDocument[];
  loading: boolean;
  currentUserId: string | null;
  /** Documents the user may file here but that are elsewhere (empty = nothing to offer). */
  movable: RagDocument[];
  onMove: (documentIds: string[]) => Promise<boolean>;
  onOpenKnowledgeBase: () => void;
  styles: Record<string, string>;
}

/** Documents filed under the matter, plus a picker to file more of the user's documents here. */
export function MatterDocumentsTab({
  documents, loading, currentUserId, movable, onMove, onOpenKnowledgeBase, styles,
}: MatterDocumentsTabProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [moving, setMoving] = useState(false);

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const submit = async () => {
    if (selected.length === 0) return;
    setMoving(true);
    const ok = await onMove(selected);
    setMoving(false);
    if (ok) { setSelected([]); setPickerOpen(false); }
  };

  return (
    <div>
      <div className={styles.tabToolbar}>
        <span className={styles.tabCount}>
          {documents.length} document{documents.length === 1 ? '' : 's'}
        </span>
        <span className={styles.spacer} />
        <button type="button" className={styles.toolBtn} onClick={onOpenKnowledgeBase}>
          <Icon name="Database" size={13} /> Knowledge Base
        </button>
        <button
          type="button"
          className={styles.toolBtnPrimary}
          onClick={() => setPickerOpen((o) => !o)}
          disabled={movable.length === 0}
          title={movable.length === 0 ? 'Every document you can file here already is' : 'Move documents into this matter'}
        >
          <Icon name="FolderInput" size={13} /> File documents here
        </button>
      </div>

      {pickerOpen && (
        <div className={styles.picker}>
          <div className={styles.pickerHead}>
            Pick documents to file under this matter. Members of the matter will be able to read them.
          </div>
          <div className={styles.pickerList}>
            {movable.map((doc) => (
              <label key={doc.id} className={styles.pickerRow}>
                <input
                  type="checkbox"
                  checked={selected.includes(doc.id)}
                  onChange={() => toggle(doc.id)}
                />
                <Icon name="FileText" size={14} className={styles.rowIcon} />
                <span className={styles.rowName}>{doc.filename}</span>
                <span className={styles.rowMeta}>{doc.folder_path || 'General'}</span>
              </label>
            ))}
          </div>
          <div className={styles.pickerActions}>
            <button type="button" className={styles.toolBtn} onClick={() => { setPickerOpen(false); setSelected([]); }}>
              Cancel
            </button>
            <button type="button" className={styles.toolBtnPrimary} onClick={submit} disabled={moving || selected.length === 0}>
              <Icon name={moving ? 'Loader2' : 'Check'} size={13} className={moving ? styles.spin : undefined} />
              Move {selected.length || ''}
            </button>
          </div>
        </div>
      )}

      {loading && documents.length === 0 ? (
        <div className={styles.empty}><Icon name="Loader2" size={16} className={styles.spin} /> Loading documents…</div>
      ) : documents.length === 0 ? (
        <div className={styles.empty}>
          <Icon name="FileText" size={18} />
          <p>No documents are filed under this matter yet.</p>
        </div>
      ) : (
        <ul className={styles.list}>
          {documents.map((doc) => (
            <li key={doc.id} className={styles.row}>
              <Icon
                name={doc.status === 'failed' || doc.status === 'delete_failed' ? 'AlertCircle' : 'FileText'}
                size={16}
                className={doc.status === 'failed' || doc.status === 'delete_failed' ? styles.rowIconError : styles.rowIcon}
              />
              <div className={styles.rowBody}>
                <div className={styles.rowName}>{doc.filename}</div>
                <div className={styles.rowMeta}>
                  {doc.status === 'delete_failed'
                    ? 'Deletion failed — retry from the Knowledge Base'
                    : doc.status === 'failed' ? 'Failed to index' : `${doc.chunk_count || 0} chunks`}
                  {doc.size ? ` · ${formatFileSize(doc.size)}` : ''}
                  {doc.created_at ? ` · ${new Date(doc.created_at).toLocaleDateString()}` : ''}
                </div>
              </div>
              <span className={`${styles.ownerBadge} ${doc.user_id === currentUserId ? styles.ownerBadgeMine : ''}`}>
                {doc.user_id === currentUserId ? 'Yours' : 'Colleague'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
