import { useEffect, useId, useState } from 'react';
import { api } from '../../../api';
import type { InstanceModels } from '../../../api/types';
import type { RagSettings } from '../../../types';

interface GenerationTabProps {
  local: RagSettings;
  setLocal: (s: RagSettings) => void;
  styles: Record<string, string>;
}

// Offered only when chat reaches OpenAI itself. With a custom endpoint (a local
// server or gateway) these names can't be served there, so the endpoint's own
// models are offered instead.
const OPENAI_MODELS = [
  { value: 'gpt-5.5', label: 'GPT-5.5' },
  { value: 'gpt-5.4-mini', label: 'GPT-5.4 Mini (Fast)' },
] as const;

const isOpenAIName = (model: string) => !/claude|gemini/i.test(model);

export const GenerationTab = ({ local, setLocal, styles }: GenerationTabProps) => {
  const id = useId();
  const [models, setModels] = useState<InstanceModels | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getInstanceModels()
      .then((m) => { if (!cancelled) setModels(m); })
      .catch(() => { /* the picker still works with the fixed list */ });
    return () => { cancelled = true; };
  }, []);

  const custom = models?.custom_endpoint ?? false;
  // Every model the admin registered; older servers send only the role models.
  const endpointModels = models && custom
    ? Array.from(new Set(models.models?.length
      ? models.models
      : [models.chat_model, models.utility_model, models.research_model, models.drafting_model].filter(Boolean)))
    : [];
  // Mirrors the server (resolve_chat_model): on a custom endpoint, an OpenAI
  // name the endpoint doesn't serve is answered by the instance default.
  const selected = local.llmModel || '';
  const value = custom && isOpenAIName(selected) && !endpointModels.includes(selected) ? '' : selected;

  return (
  <div className={styles.settingsGrid}>
    <div className={styles.settingGroup}>
      <label className={styles.settingLabel} htmlFor={`${id}-llm-model`}>LLM Model</label>
      <select id={`${id}-llm-model`} className={styles.select} value={value} onChange={e => setLocal({...local, llmModel: e.target.value})}>
        <option value="">{models ? `Instance default (${models.chat_model})` : 'Instance default'}</option>
        {custom ? (
          <optgroup label="Your model server">
            {endpointModels.map((m) => <option key={m} value={m}>{m}</option>)}
          </optgroup>
        ) : (
          <optgroup label="OpenAI">
            {OPENAI_MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </optgroup>
        )}
        <optgroup label="Anthropic">
          <option value="claude-opus-4-8">Claude Opus 4.8</option>
          <option value="claude-sonnet-4-6">Claude Sonnet 4.6</option>
          <option value="claude-haiku-4-5">Claude Haiku 4.5 (Fast)</option>
        </optgroup>
        <optgroup label="Google">
          <option value="gemini-3.1-pro-preview">Gemini 3.1 Pro</option>
        </optgroup>
      </select>
      <span className={styles.helpText}>
        {custom
          ? 'Your administrator connected a model server. The instance default uses it.'
          : 'The instance default is the model your administrator set.'}
      </span>
    </div>
    <div className={styles.settingGroup}>
      <div className={styles.sliderHeader}>
        <label className={styles.settingLabel} htmlFor={`${id}-temperature`}>Temperature</label>
        <span className={`${styles.sliderValue} mono`}>{(local.temperature ?? 0.1).toFixed(2)}</span>
      </div>
      {/* `??`, not `||`: 0 (fully deterministic) is a valid choice. */}
      <input id={`${id}-temperature`} type="range" min="0" max="1" step="0.05" value={local.temperature ?? 0.1} onChange={e => setLocal({...local, temperature: parseFloat(e.target.value)})} />
    </div>
    <div className={styles.settingGroup}>
      <div className={styles.sliderHeader}>
        <label className={styles.settingLabel} htmlFor={`${id}-max-tokens`}>Max Tokens</label>
        <span className={`${styles.sliderValue} mono`}>{local.maxTokens || 4096}</span>
      </div>
      {/* 4096 is the server default; the range covers values saved by earlier builds. */}
      <input id={`${id}-max-tokens`} type="range" min="1024" max="32768" step="512" value={local.maxTokens || 4096} onChange={e => setLocal({...local, maxTokens: parseInt(e.target.value)})} />
    </div>
  </div>
  );
};
