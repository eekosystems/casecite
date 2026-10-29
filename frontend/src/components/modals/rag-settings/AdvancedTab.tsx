import { useSettingsStore } from '../../../stores/settingsStore';
import { Icon } from '../../shared/Icon';
import type { RagSettings } from '../../../types';

interface AdvancedTabProps {
  local: RagSettings;
  setLocal: (s: RagSettings) => void;
  styles: Record<string, string>;
}

export const AdvancedTab = ({ local, setLocal, styles }: AdvancedTabProps) => {
  const { isReindexing, handleReindex } = useSettingsStore();

  return (
  <div className={styles.toggleList}>
    {[
      { key: 'enableReranking', label: 'Cross-Encoder Reranking', desc: 'Rerank retrieved passages with a cross-encoder model. Takes effect only when the server has the optional sentence-transformers package installed; otherwise it is ignored and results keep their similarity order.' },
      { key: 'hybridSearch', label: 'Keyword Rescoring (BM25)', desc: 'Blend keyword-match scores into the ranking of the passages that semantic search retrieved' },
      { key: 'citationVerification', label: 'Citation Verification', desc: 'Cross-reference citations against source documents' },
      { key: 'contextCompression', label: 'Context Compression', desc: 'Compress retrieved context to fit more documents' },
      { key: 'queryExpansion', label: 'Query Expansion', desc: 'Automatically expand queries with related terms' },
      { key: 'sourceTracking', label: 'Source Tracking', desc: 'Track exact source locations for all citations' },
      { key: 'workflow_email_notifications', label: 'Workflow Emails', desc: 'Email you when a workflow run you started finishes or fails, and when a run needs your approval. Only when the server has workflow notifications switched on.' },
    ].map((item: { key: string; label: string; desc: string }) => (
      <div key={item.key} className={styles.toggleItem}>
        <div>
          <div className={styles.toggleLabel}>{item.label}</div>
          <div className={styles.toggleDesc}>{item.desc}</div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={!!local[item.key]}
          aria-label={item.label}
          className={`${styles.toggleSwitch} ${local[item.key] ? styles.toggleSwitchOn : styles.toggleSwitchOff}`}
          onClick={() => setLocal({...local, [item.key]: !local[item.key]})}
        >
          <div className={`${styles.toggleKnob} ${local[item.key] ? styles.toggleKnobOn : styles.toggleKnobOff}`} />
        </button>
      </div>
    ))}
    <div className={styles.toggleItem}>
      <div>
        <div className={styles.toggleLabel}>Rebuild Search Index</div>
        <div className={styles.toggleDesc}>
          Re-embed every one of your documents from its stored file. Use this when documents
          show as indexed but chat can't find them.
        </div>
      </div>
      <button className={styles.reindexBtn} onClick={handleReindex} disabled={isReindexing}>
        {isReindexing ? (
          <><Icon name="Loader2" size={14} className={styles.spin} /> Rebuilding…</>
        ) : (
          <><Icon name="RefreshCw" size={14} /> Rebuild</>
        )}
      </button>
    </div>
  </div>
  );
};
