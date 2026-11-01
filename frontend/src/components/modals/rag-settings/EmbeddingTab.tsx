import { useCallback, useEffect, useId, useState } from 'react';
import { api } from '../../../api';
import { useUIStore } from '../../../stores/uiStore';
import { InfoTip } from '../../shared/InfoTip';
import type { EmbeddingInfo, EmbeddingReindexStatus, EmbeddingStatus } from '../../../api/types';

interface EmbeddingTabProps {
  /** Admins can change the model; everyone else sees what is in use. */
  isAdmin: boolean;
  styles: Record<string, string>;
}

const PROVIDER_LABELS: Record<string, string> = {
  self_hosted: 'on your own server',
  openai: 'by OpenAI',
  voyage: 'by Voyage AI',
  cohere: 'by Cohere',
};

const providerLabel = (provider: string) => PROVIDER_LABELS[provider] ?? `by ${provider}`;

// Progress of re-embedding while documents move to a new model.
const ReindexLine = ({ reindex, styles }: { reindex: Pick<EmbeddingReindexStatus, 'state' | 'done' | 'total' | 'failed'>; styles: Record<string, string> }) => {
  if (reindex.state === 'running') {
    return (
      <p className={styles.bannerTextAmber} role="status">
        Re-indexing documents with the new model: {reindex.done} of {reindex.total || '…'} done.
        Meaning-based search finds a document again once it is done; keyword search finds it throughout.
      </p>
    );
  }
  if (reindex.failed > 0) {
    return (
      <p className={styles.bannerTextAmber} role="status">
        {reindex.failed} {reindex.failed === 1 ? 'document' : 'documents'} could not be re-indexed. Each one shows the reason in the knowledge base.
      </p>
    );
  }
  return null;
};

export const EmbeddingTab = ({ isAdmin, styles }: EmbeddingTabProps) => {
  const id = useId();
  const addToast = useUIStore((st) => st.addToast);
  const showConfirm = useUIStore((st) => st.showConfirm);

  const [info, setInfo] = useState<EmbeddingInfo | null>(null);
  const [status, setStatus] = useState<EmbeddingStatus | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [dimensions, setDimensions] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      if (isAdmin) {
        const st = await api.getEmbeddingStatus();
        setStatus(st);
        return st;
      }
      setInfo(await api.getEmbeddingInfo());
    } catch { /* shown as "not available" below */ }
    return null;
  }, [isAdmin]);

  useEffect(() => {
    load().then((st) => {
      if (st && st.source === 'instance') {
        setBaseUrl(st.base_url || '');
        setModel(st.model);
      }
    });
  }, [load]);

  // Follow the re-embedding while it runs.
  const running = (status?.reindex.state ?? info?.reindex_state) === 'running';
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => { load(); }, 3000);
    return () => clearInterval(timer);
  }, [running, load]);

  const save = async () => {
    setSaving(true);
    try {
      const saved = await api.setEmbeddings({
        baseUrl: baseUrl.trim(),
        model: model.trim(),
        dimensions: dimensions ? parseInt(dimensions, 10) : null,
        apiKey: apiKey.trim() || undefined,
      });
      setStatus(saved);
      setApiKey('');
      setDimensions('');
      addToast(`Embedding model saved. Re-indexing documents with ${saved.model}.`, 'success');
    } catch (e) {
      addToast(e instanceof Error ? e.message : 'The embedding server could not be used', 'error');
    } finally {
      setSaving(false);
    }
  };

  const confirmSave = () => {
    showConfirm({
      title: 'Change the embedding model',
      message:
        'The server is checked first. Then every document is re-embedded with the new model in the background. '
        + 'Until a document is done, keyword search still finds it but meaning-based search does not. '
        + 'Switching back to a model used before is quick.',
      type: 'warning',
      confirmText: 'Save and re-index',
      onConfirm: () => { save(); },
    });
  };

  const revert = () => {
    showConfirm({
      title: 'Use the .env embedding settings',
      message: 'Documents go back to the embedding model set in the server\'s .env. Documents already embedded with it are searchable again right away.',
      type: 'warning',
      confirmText: 'Switch back',
      onConfirm: async () => {
        try {
          setStatus(await api.clearEmbeddings());
          setBaseUrl('');
          setModel('');
          addToast('Using the .env embedding settings', 'success');
        } catch (e) {
          addToast(e instanceof Error ? e.message : 'Failed to switch back', 'error');
        }
      },
    });
  };

  const retry = async () => {
    try {
      setStatus(await api.reindexEmbeddings());
    } catch (e) {
      addToast(e instanceof Error ? e.message : 'Failed to start re-indexing', 'error');
    }
  };

  const current = status ?? info;
  const reindex = status
    ? status.reindex
    : info && { state: info.reindex_state, done: info.reindex_done, total: info.reindex_total, failed: 0 };
  const canSave = baseUrl.trim().length >= 4 && model.trim().length > 0 && !saving;

  return (
    <div className={styles.settingsGrid}>
      <div className={`${styles.settingGroup} ${styles.fullWidth}`}>
        <div className={styles.labelRow}>
          <span className={styles.settingLabel}>Embedding model in use</span>
          <InfoTip label="About embeddings">
            Embeddings turn text into numbers so search can find passages by meaning, not only by exact words.
            Every document and every question is embedded with this model, and the text of each uploaded
            document is sent to the provider named here when it is indexed.
          </InfoTip>
        </div>
        {current ? (
          <p className={styles.summaryText}>
            Documents are embedded with <strong className="mono">{current.model}</strong> ({current.dimensions} dimensions)
            {' '}{providerLabel(current.provider)}.{' '}
            {current.source === 'instance' ? 'Set by an administrator here.' : 'Set in the server\'s .env.'}
          </p>
        ) : (
          <p className={styles.helpText}>Embedding settings are not available right now.</p>
        )}
        {reindex && <ReindexLine reindex={reindex} styles={styles} />}
        {!isAdmin && (
          <span className={styles.helpText}>Your administrator manages the embedding model.</span>
        )}
      </div>

      {isAdmin && status && !status.editable && (
        <p className={styles.bannerTextAmber}>
          This instance stores vectors in Pinecone, whose index has one fixed vector size, so the
          embedding model is set in .env (EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS).
        </p>
      )}

      {isAdmin && status?.editable && (
        <div className={styles.localModelBox}>
          <p className={styles.localModelDesc}>
            Use any OpenAI-compatible embeddings server — Ollama (e.g. nomic-embed-text, mxbai-embed-large),
            vLLM, LM Studio or text-embeddings-inference — so document text is embedded on your own network.
          </p>
          <div>
            <div className={styles.labelRow}>
              <label className={styles.settingLabel} htmlFor={`${id}-emb-url`}>Server URL</label>
              <InfoTip label="About the embedding server URL">
                An OpenAI-compatible embeddings endpoint, usually ending in /v1 (Ollama:
                http://localhost:11434/v1). It can differ from your chat model servers. The server is sent a
                test request before anything changes.
              </InfoTip>
            </div>
            <input
              id={`${id}-emb-url`}
              className={styles.modalInputMono}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://localhost:11434/v1"
            />
          </div>
          <div>
            <div className={styles.labelRow}>
              <label className={styles.settingLabel} htmlFor={`${id}-emb-model`}>Model</label>
              <InfoTip label="About the embedding model">
                The embedding model&apos;s name exactly as your server lists it, for example nomic-embed-text,
                mxbai-embed-large or Qwen3-Embedding-8B. It must be an embedding model; a chat model fails the
                server check.
              </InfoTip>
            </div>
            <input
              id={`${id}-emb-model`}
              className={styles.modalInputMono}
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="nomic-embed-text"
            />
          </div>
          <div>
            <div className={styles.labelRow}>
              <label className={styles.settingLabel} htmlFor={`${id}-emb-dims`}>Dimensions (optional)</label>
              <InfoTip label="About embedding dimensions">
                How many numbers the model returns per piece of text. Leave it blank and it is read from the
                server&apos;s answer. If you fill it in and the server returns a different size, saving is
                refused, which catches a wrong model name.
              </InfoTip>
            </div>
            <input
              id={`${id}-emb-dims`}
              type="number"
              inputMode="numeric"
              className={styles.modalInputMono}
              value={dimensions}
              onChange={(e) => setDimensions(e.target.value)}
              placeholder="detected from the server"
            />
          </div>
          <div>
            <div className={styles.labelRow}>
              <label className={styles.settingLabel} htmlFor={`${id}-emb-key`}>API key (optional)</label>
              <InfoTip label="About the embedding server key">
                Only if your server needs one; local servers usually don&apos;t. It is stored encrypted and
                sent only to this server, never the instance&apos;s OpenAI key. Leave it blank to keep a key
                that is already saved.
              </InfoTip>
            </div>
            <input
              id={`${id}-emb-key`}
              type="password"
              className={styles.modalInputMono}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={status.api_key_set ? 'saved — type to replace' : 'only if your server needs one'}
              autoComplete="off"
            />
          </div>
          <div className={styles.boxActions}>
            {status.source === 'instance' && (
              <span className={styles.actionWithTip}>
                <button className={styles.brandingResetBtn} onClick={revert}>Use .env settings</button>
                <InfoTip label="What using the .env settings does">
                  Goes back to the embedding model in the server&apos;s .env. Documents already embedded with it
                  are searchable again at once; only documents added since then are re-embedded.
                </InfoTip>
              </span>
            )}
            {status.reindex.state !== 'running' && status.reindex.failed > 0 && (
              <span className={styles.actionWithTip}>
                <button className={styles.btnSecondarySmall} onClick={retry}>Retry failed documents</button>
                <InfoTip label="What retrying does">
                  Re-embeds the documents that failed, for example because the server was down. Each failed
                  document shows its reason in the knowledge base.
                </InfoTip>
              </span>
            )}
            <span className={styles.actionWithTip}>
              <button className={styles.btnPrimary} onClick={confirmSave} disabled={!canSave}>
                {saving ? 'Checking server…' : 'Save and re-index'}
              </button>
              <InfoTip label="What saving does" tone="warning">
                The server is tested first; if it fails, nothing changes. Then every document is re-embedded
                with the new model in the background, which can take a while for a large library and keeps the
                server busy. Until a document is done, keyword search finds it but meaning-based search
                doesn&apos;t. An upload in progress at that moment may need uploading again. Switching back to
                a model used before is quick.
              </InfoTip>
            </span>
          </div>
        </div>
      )}
    </div>
  );
};
