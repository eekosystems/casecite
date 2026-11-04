import { useState } from 'react';
import { Icon } from './Icon';

interface MatterGovernancePanelProps {
  legalHold: boolean;
  legalHoldReason: string | null;
  legalHoldSetAt: string | null;
  retentionDays: number | null;
  onSetHold: (on: boolean, reason: string | null) => Promise<boolean>;
  /** 0 clears the policy. */
  onSetRetention: (days: number) => Promise<boolean>;
  styles: Record<string, string>;
}

/**
 * Legal hold and retention for a matter (owner only).
 *
 * A hold freezes the matter: nothing filed under it can be deleted by anyone
 * through any path until it is lifted, and retention is suspended. Retention
 * removes documents and conversations older than N days, daily, through the
 * normal delete path.
 */
export function MatterGovernancePanel({
  legalHold, legalHoldReason, legalHoldSetAt, retentionDays, onSetHold, onSetRetention, styles: s,
}: MatterGovernancePanelProps) {
  const [reason, setReason] = useState('');
  const [days, setDays] = useState<string>(retentionDays ? String(retentionDays) : '');
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<boolean>) => {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  };

  const submitRetention = (e: React.FormEvent) => {
    e.preventDefault();
    const n = days.trim() === '' ? 0 : Number(days);
    if (!Number.isFinite(n) || n < 0) return;
    run(() => onSetRetention(Math.round(n)));
  };

  return (
    <section
      className={`${s.governance} ${legalHold ? s.governanceOnHold : ''}`}
      aria-label="Legal hold and retention"
      data-testid="matter-governance"
    >
      <div className={s.governanceRow}>
        <span className={s.governanceTitle}>
          <Icon name={legalHold ? 'Lock' : 'Unlock'} size={14} /> Legal hold
        </span>
        {legalHold ? (
          <>
            <span className={s.governanceText}>
              On hold{legalHoldSetAt ? ` since ${new Date(legalHoldSetAt).toLocaleDateString()}` : ''}
              {legalHoldReason ? `: ${legalHoldReason}` : ''}. Nothing filed under this matter can be
              deleted by anyone, and retention is suspended, until the hold is lifted.
            </span>
            <button className={s.secondaryBtn} disabled={busy} onClick={() => run(() => onSetHold(false, null))}>
              <Icon name="Unlock" size={14} /> Lift hold
            </button>
          </>
        ) : (
          <>
            <input
              className={s.governanceReason}
              aria-label="Hold reason"
              placeholder="Reason (e.g. litigation hold, subpoena)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <button className={s.dangerBtn} disabled={busy} onClick={() => run(() => onSetHold(true, reason.trim() || null))}>
              <Icon name="Lock" size={14} /> Place hold
            </button>
          </>
        )}
      </div>

      <form className={s.governanceRow} onSubmit={submitRetention}>
        <span className={s.governanceTitle}>
          <Icon name="CalendarClock" size={14} /> Retention
        </span>
        <span className={s.governanceText}>
          {retentionDays
            ? `Documents and conversations filed here are removed once older than ${retentionDays} days (daily, audited).`
            : 'Everything filed here is kept until deleted. Set a number of days to remove older documents and conversations automatically.'}
        </span>
        <input
          className={s.governanceInput}
          aria-label="Retention days"
          type="number"
          min={0}
          max={10950}
          placeholder="days"
          value={days}
          onChange={(e) => setDays(e.target.value)}
          disabled={legalHold}
        />
        <button className={s.secondaryBtn} type="submit" disabled={busy || legalHold}>
          {days.trim() === '' || Number(days) === 0 ? 'Keep everything' : 'Apply'}
        </button>
      </form>
    </section>
  );
}

export default MatterGovernancePanel;
