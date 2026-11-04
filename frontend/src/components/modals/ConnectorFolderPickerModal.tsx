import { useEffect, useId, useState } from 'react';
import { api } from '../../api';
import type { ConnectorFolder } from '../../api/types';
import { useConnectorsStore } from '../../stores/connectorsStore';
import s from './FilePickerModal.module.css';
import { Icon } from '../shared/Icon';
import { ModalShell } from '../shared/ModalShell';

interface Crumb {
  id: string | null;
  name: string;
}

/**
 * Choose which folder of a connected account to import.
 *
 * Browses the account's folder tree through the server (the connector's own
 * listing, folders only), and starts a sync scoped to the folder the user
 * picks. Importing the whole account stays possible, behind the same explicit
 * warning as before.
 */
function ConnectorFolderPickerModal() {
  const titleId = useId();
  const { folderPicker, closeFolderPicker, syncFolder, syncWholeAccount } = useConnectorsStore();
  const [crumbs, setCrumbs] = useState<Crumb[]>([{ id: null, name: 'All folders' }]);
  const [folders, setFolders] = useState<ConnectorFolder[]>([]);
  const [nextPage, setNextPage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ConnectorFolder | null>(null);

  const connector = folderPicker?.connector ?? null;
  const current = crumbs[crumbs.length - 1];

  useEffect(() => {
    if (!connector) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSelected(null);
    api.listConnectorFolders(connector.id, current.id ?? undefined)
      .then((resp) => {
        if (cancelled) return;
        setFolders(resp.folders);
        setNextPage(resp.next_page_token ?? null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Could not list folders');
        setFolders([]);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [connector, current.id]);

  if (!connector) return null;

  const loadMore = async () => {
    if (!nextPage) return;
    setLoading(true);
    try {
      const resp = await api.listConnectorFolders(connector.id, current.id ?? undefined, nextPage);
      setFolders((prev) => [...prev, ...resp.folders]);
      setNextPage(resp.next_page_token ?? null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not list folders');
    } finally {
      setLoading(false);
    }
  };

  const open = (folder: ConnectorFolder) => {
    setCrumbs((prev) => [...prev, { id: folder.id, name: folder.name }]);
  };

  const importTarget = selected ?? (current.id ? { id: current.id, name: current.name, path: null } : null);

  return (
    <ModalShell onClose={closeFolderPicker} overlayClassName={s.modalOverlay} className={s.modalContainerPicker} labelledBy={titleId}>
      <div className={s.modalHeader}>
        <div className={s.modalHeaderLeft}>
          <div className={s.modalIcon} style={{ background: connector.color || 'var(--navy-800)' }}>
            <Icon name={connector.icon || 'Cloud'} size={20} className={s.iconWhite} />
          </div>
          <div>
            <h2 id={titleId} className={s.modalTitle}>Import from {connector.name}</h2>
            <p className={s.modalSubtitle}>Choose the folder to import</p>
          </div>
        </div>
        <button className={s.modalClose} onClick={closeFolderPicker} aria-label="Close">
          <Icon name="X" size={20} />
        </button>
      </div>

      <div className={s.pickerBody}>
        <nav className={s.folderCrumbs} aria-label="Folder path">
          {crumbs.map((c, i) => (
            <span key={`${c.id ?? 'root'}-${i}`} className={s.folderCrumb}>
              {i > 0 && <Icon name="ChevronRight" size={12} />}
              <button
                type="button"
                className={s.folderCrumbBtn}
                disabled={i === crumbs.length - 1}
                onClick={() => setCrumbs(crumbs.slice(0, i + 1))}
              >
                {c.name}
              </button>
            </span>
          ))}
        </nav>

        {error && <div className={s.folderError} role="alert">{error}</div>}

        <ul className={s.folderList} aria-label="Folders" data-testid="connector-folders">
          {folders.map((f) => (
            <li key={f.id} className={`${s.folderRow} ${selected?.id === f.id ? s.folderRowSelected : ''}`}>
              <button
                type="button"
                className={s.folderSelect}
                aria-pressed={selected?.id === f.id}
                onClick={() => setSelected(selected?.id === f.id ? null : f)}
              >
                <Icon name="Folder" size={16} /> {f.name}
              </button>
              <button type="button" className={s.folderOpen} onClick={() => open(f)} aria-label={`Open ${f.name}`}>
                Open <Icon name="ChevronRight" size={14} />
              </button>
            </li>
          ))}
          {!loading && folders.length === 0 && !error && (
            <li className={s.folderEmpty}>No subfolders here.</li>
          )}
        </ul>
        {loading && <div className={s.folderLoading}><Icon name="Loader2" size={16} className={s.spinnerIcon} /> Loading…</div>}
        {nextPage && !loading && (
          <button type="button" className={s.folderMore} onClick={loadMore}>Load more</button>
        )}

        <div className={s.pickerDisclaimer}>
          Every supported document in the chosen folder and its subfolders is downloaded and
          indexed; the text of each is sent to your configured embedding provider.
        </div>

        <div className={s.folderActions}>
          <button
            type="button"
            className={s.browseBtn}
            style={{ background: connector.color || 'var(--navy-700)' }}
            disabled={!importTarget}
            onClick={() => importTarget && syncFolder(connector, importTarget.id, importTarget.name)}
          >
            <Icon name="Download" size={16} />
            {importTarget ? `Import "${importTarget.name}"` : 'Select a folder, or open one, to import'}
          </button>
          <button type="button" className={s.folderWholeAccount} onClick={() => syncWholeAccount(connector)}>
            Import the entire account instead…
          </button>
        </div>
      </div>
    </ModalShell>
  );
}

export { ConnectorFolderPickerModal };
