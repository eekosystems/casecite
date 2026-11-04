import type { Citation, VerificationState } from '../types';

export type VerificationTone = 'good' | 'warn' | 'bad' | 'muted';

export interface VerificationBadge {
  /** Short label for a badge. */
  label: string;
  /** One sentence for a tooltip or a line under the badge. */
  title: string;
  tone: VerificationTone;
  /** Style key: the class a stylesheet maps each tone to. */
  className: 'verifiedBadge' | 'unverifiedBadge' | 'contradictedBadge' | 'notCheckedBadge';
}

const CLASS_FOR_TONE: Record<VerificationTone, VerificationBadge['className']> = {
  good: 'verifiedBadge',
  warn: 'unverifiedBadge',
  bad: 'contradictedBadge',
  muted: 'notCheckedBadge',
};

const BY_STATE: Record<VerificationState, Omit<VerificationBadge, 'className'>> = {
  supported: {
    label: 'Supported',
    title: 'The quoted passage was found verbatim in the source and, read in context, supports this statement.',
    tone: 'good',
  },
  partial: {
    label: 'Partly supported',
    title: 'The quoted passage is real, but the source adds a condition, exception or threshold the answer leaves out.',
    tone: 'warn',
  },
  contradicted: {
    label: 'Contradicted',
    title: 'The quoted passage is real, but the source says something different from the answer.',
    tone: 'bad',
  },
  unsupported: {
    label: 'Unsupported',
    title: 'The quoted passage does not establish this statement.',
    tone: 'bad',
  },
  quote_only: {
    label: 'Quote verified · support not checked',
    title: 'The quotation exists in the source, but whether it supports the statement was not assessed (the support check is off or could not run).',
    tone: 'warn',
  },
  quote_unverified: {
    label: 'Quote not found',
    title: 'The quoted passage could not be found verbatim in the source file. Treat this point as unverified.',
    tone: 'warn',
  },
  not_checked: {
    label: 'Not checked',
    title: 'This passage matched your question at retrieval. No statement in the answer was checked against it.',
    tone: 'muted',
  },
};

/**
 * The verification badge for a citation, or null when there is nothing to say.
 *
 * Document citations from the research chat carry an explicit `verification`
 * state and get its badge. Citations with only the older boolean `verified`
 * (deep-research authorities, strategy briefs) keep the binary badge. A
 * citation with neither shows nothing rather than an invented state.
 */
export function verificationBadge(
  cite: Pick<Citation, 'verification' | 'verified'>,
): VerificationBadge | null {
  const state = cite.verification;
  if (state && state in BY_STATE) {
    const base = BY_STATE[state];
    return { ...base, className: CLASS_FOR_TONE[base.tone] };
  }
  if (cite.verified === true) {
    return {
      label: 'Verified',
      title: 'The quoted passage was verified verbatim against the source.',
      tone: 'good',
      className: 'verifiedBadge',
    };
  }
  if (cite.verified === false) {
    return {
      label: 'Unverified',
      title: 'The quoted passage could not be verified against the source.',
      tone: 'warn',
      className: 'unverifiedBadge',
    };
  }
  return null;
}
