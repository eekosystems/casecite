import { useState } from 'react';
import { Icon } from './Icon';
import type { MatterMember, MatterWall } from '../../api/types';

interface MatterMembersTabProps {
  members: MatterMember[];
  isOwner: boolean;
  isPersonal: boolean;
  currentUserId: string | null;
  onAdd: (email: string) => Promise<boolean>;
  onRemove: (userId: string, label: string) => void;
  onTransfer: (userId: string, label: string) => void;
  styles: Record<string, string>;
  /** Admin-only ethical walls; omitted for everyone else. */
  isAdmin?: boolean;
  walls?: MatterWall[];
  onAddWall?: (email: string, reason: string | null) => Promise<boolean>;
  onRemoveWall?: (userId: string) => Promise<boolean>;
}

const memberLabel = (m: MatterMember) => m.name || m.email || m.user_id;

/** Who can see the matter; the owner invites by email, removes, or hands the matter over. */
export function MatterMembersTab({
  members, isOwner, isPersonal, currentUserId, onAdd, onRemove, onTransfer, styles,
  isAdmin = false, walls = [], onAddWall, onRemoveWall,
}: MatterMembersTabProps) {
  const [email, setEmail] = useState('');
  const [adding, setAdding] = useState(false);
  const [wallEmail, setWallEmail] = useState('');
  const [wallReason, setWallReason] = useState('');
  const [walling, setWalling] = useState(false);

  const submitWall = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!onAddWall || !wallEmail.trim()) return;
    setWalling(true);
    const ok = await onAddWall(wallEmail, wallReason.trim() || null);
    setWalling(false);
    if (ok) { setWallEmail(''); setWallReason(''); }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim()) return;
    setAdding(true);
    const ok = await onAdd(email);
    setAdding(false);
    if (ok) setEmail('');
  };

  if (isPersonal) {
    return (
      <div className={styles.empty}>
        <Icon name="Lock" size={18} />
        <p>Your personal matter is private. Create a shared matter to work with colleagues.</p>
      </div>
    );
  }

  return (
    <div>
      {isOwner && (
        <form className={styles.inviteForm} onSubmit={submit}>
          <input
            type="email"
            className={styles.input}
            placeholder="colleague@firm.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-label="Colleague's email address"
          />
          <button type="submit" className={styles.toolBtnPrimary} disabled={adding || !email.trim()}>
            <Icon name={adding ? 'Loader2' : 'UserPlus'} size={13} className={adding ? styles.spin : undefined} />
            Add member
          </button>
          <span className={styles.hint}>They need an active account on this instance.</span>
        </form>
      )}
      <ul className={styles.list}>
        {members.map((m) => {
          const isSelf = m.user_id === currentUserId;
          const label = memberLabel(m);
          return (
            <li key={m.user_id} className={styles.row}>
              <div className={styles.avatar}>{(m.name || m.email || '?')[0]?.toUpperCase()}</div>
              <div className={styles.rowBody}>
                <div className={styles.rowName}>
                  {label}{isSelf ? ' (you)' : ''}
                </div>
                {m.email && m.name && <div className={styles.rowMeta}>{m.email}</div>}
              </div>
              <span className={`${styles.roleBadge} ${m.role === 'owner' ? styles.roleOwner : ''}`}>{m.role}</span>
              {isOwner && m.role !== 'owner' && (
                <>
                  <button type="button" className={styles.toolBtn} onClick={() => onTransfer(m.user_id, label)} title="Make this member the owner">
                    <Icon name="KeyRound" size={13} /> Make owner
                  </button>
                  <button type="button" className={styles.toolBtnDanger} onClick={() => onRemove(m.user_id, label)} title="Remove from matter">
                    <Icon name="UserMinus" size={13} /> Remove
                  </button>
                </>
              )}
            </li>
          );
        })}
      </ul>

      {isAdmin && onAddWall && onRemoveWall && (
        <section className={styles.wallSection} aria-label="Ethical walls" data-testid="ethical-walls">
          <div className={styles.wallTitle}>
            <Icon name="ShieldOff" size={14} /> Ethical walls
          </div>
          <p className={styles.hint}>
            A walled user can never see this matter: they are removed as a member, cannot be added back,
            and any work they filed here passes to the matter's owner. Admins only.
          </p>
          <form className={styles.inviteForm} onSubmit={submitWall}>
            <input
              type="email"
              className={styles.input}
              placeholder="conflicted@firm.com"
              value={wallEmail}
              onChange={(e) => setWallEmail(e.target.value)}
              aria-label="Email of the user to wall off"
            />
            <input
              type="text"
              className={styles.input}
              placeholder="Reason (e.g. prior representation of the other side)"
              value={wallReason}
              onChange={(e) => setWallReason(e.target.value)}
              aria-label="Wall reason"
            />
            <button type="submit" className={styles.toolBtnDanger} disabled={walling || !wallEmail.trim()}>
              <Icon name={walling ? 'Loader2' : 'ShieldOff'} size={13} className={walling ? styles.spin : undefined} />
              Wall off
            </button>
          </form>
          {walls.length > 0 && (
            <ul className={styles.list}>
              {walls.map((w) => (
                <li key={w.user_id} className={styles.row}>
                  <div className={styles.avatar}><Icon name="ShieldOff" size={13} /></div>
                  <div className={styles.rowBody}>
                    <div className={styles.rowName}>{w.name || w.email || w.user_id}</div>
                    <div className={styles.rowMeta}>
                      {w.email && w.name ? `${w.email} · ` : ''}
                      {w.reason ? w.reason : 'No reason recorded'}
                      {` · since ${new Date(w.created_at).toLocaleDateString()}`}
                    </div>
                  </div>
                  <button type="button" className={styles.toolBtn} onClick={() => onRemoveWall(w.user_id)} title="Lift the wall">
                    <Icon name="Shield" size={13} /> Lift wall
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
