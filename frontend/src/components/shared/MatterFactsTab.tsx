import { useId } from 'react';
import { Icon } from './Icon';
import { InfoTip } from './InfoTip';
import { parseUtcDate } from '../../utils/helpers';
import type { MatterMember } from '../../api/types';

interface MatterFactsTabProps {
  facts: string | null;
  /** Unsaved edits, or null when the text matches what is saved. */
  draft: string | null;
  updatedAt: string | null;
  updatedBy: string | null;
  members: MatterMember[];
  maxChars: number;
  saving: boolean;
  readOnly: boolean;
  onChange: (text: string) => void;
  onSave: () => void;
  styles: Record<string, string>;
}

const PLACEHOLDER = [
  'Write the facts of the matter in your own words, for example:',
  '- Parties and roles: who the client is, who is on the other side',
  '- Key dates: contract signed, notices sent, deadlines',
  '- What happened, in order',
  "- The client's goals and constraints",
  '- Open questions to resolve',
].join('\n');

const UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['week', 7 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
];

function relativeTime(timestamp: string): string {
  const seconds = (parseUtcDate(timestamp).getTime() - Date.now()) / 1000;
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
  }
  return 'just now';
}

const memberName = (members: MatterMember[], userId: string | null) => {
  const member = members.find((m) => m.user_id === userId);
  return member?.name || member?.email || 'a member';
};

/** The case team's account of the matter; every chat and strategy brief in it reads this. */
export function MatterFactsTab({
  facts, draft, updatedAt, updatedBy, members, maxChars, saving, readOnly, onChange, onSave, styles,
}: MatterFactsTabProps) {
  const id = useId();
  const text = draft ?? facts ?? '';
  const dirty = draft !== null;
  const over = text.length > maxChars;

  return (
    <div className={styles.factsPanel}>
      <div className={styles.factsHeader}>
        <label className={styles.factsTitle} htmlFor={`${id}-facts`}>Facts</label>
        <InfoTip label="About the matter facts">
          Chats about this matter (filed under it, or with the matter selected as the chat&apos;s scope) and their
          strategy briefs include these facts as your team&apos;s own account of the matter. They are never cited
          as a source. Every member of the matter can read them; members who can add documents can edit them.
        </InfoTip>
        {dirty && <span className={styles.factsUnsaved}>Unsaved changes</span>}
      </div>

      {readOnly && (
        <p className={styles.factsReadOnly}>
          <Icon name="Lock" size={13} /> Your role can read these facts but not edit them.
        </p>
      )}

      <textarea
        id={`${id}-facts`}
        className={styles.factsText}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        placeholder={readOnly ? 'No facts have been written for this matter yet.' : PLACEHOLDER}
        readOnly={readOnly}
        maxLength={maxChars}
        rows={14}
        aria-describedby={`${id}-count`}
      />

      <div className={styles.factsFooter}>
        <span id={`${id}-count`} className={`${styles.factsCount} ${over ? styles.factsCountOver : ''}`}>
          {text.length.toLocaleString()} / {maxChars.toLocaleString()} characters
        </span>
        {updatedAt && (
          <span className={styles.hint}>
            Last edited {relativeTime(updatedAt)} by {memberName(members, updatedBy)}
          </span>
        )}
        <span className={styles.spacer} />
        {!readOnly && (
          <button
            type="button"
            className={styles.toolBtnPrimary}
            onClick={onSave}
            disabled={!dirty || saving || over}
          >
            <Icon name={saving ? 'Loader2' : 'Save'} size={13} className={saving ? styles.spin : undefined} />
            {saving ? 'Saving…' : 'Save'}
          </button>
        )}
      </div>
    </div>
  );
}
