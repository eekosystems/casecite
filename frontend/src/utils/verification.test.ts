import { describe, it, expect } from 'vitest';
import { verificationBadge } from './verification';

describe('verificationBadge', () => {
  it('maps every explicit state to a label and tone', () => {
    expect(verificationBadge({ verification: 'supported' })).toMatchObject({ label: 'Supported', tone: 'good' });
    expect(verificationBadge({ verification: 'partial' })).toMatchObject({ label: 'Partly supported', tone: 'warn' });
    expect(verificationBadge({ verification: 'contradicted' })).toMatchObject({ label: 'Contradicted', tone: 'bad' });
    expect(verificationBadge({ verification: 'unsupported' })).toMatchObject({ label: 'Unsupported', tone: 'bad' });
    expect(verificationBadge({ verification: 'quote_only' })).toMatchObject({
      label: 'Quote verified · support not checked',
      tone: 'warn',
    });
    expect(verificationBadge({ verification: 'quote_unverified' })).toMatchObject({ label: 'Quote not found', tone: 'warn' });
    expect(verificationBadge({ verification: 'not_checked' })).toMatchObject({ label: 'Not checked', tone: 'muted' });
  });

  it('prefers the explicit state over the boolean flag', () => {
    // A verbatim quote whose support was not assessed must not read as plain "Verified".
    expect(verificationBadge({ verification: 'quote_only', verified: true })?.label)
      .toBe('Quote verified · support not checked');
  });

  it('keeps the binary badge for citations that only carry the flag', () => {
    expect(verificationBadge({ verified: true })).toMatchObject({ label: 'Verified', className: 'verifiedBadge' });
    expect(verificationBadge({ verified: false })).toMatchObject({ label: 'Unverified', className: 'unverifiedBadge' });
  });

  it('shows nothing when there is nothing to say', () => {
    expect(verificationBadge({})).toBeNull();
    expect(verificationBadge({ verification: 'bogus' as never })).toBeNull();
  });
});
