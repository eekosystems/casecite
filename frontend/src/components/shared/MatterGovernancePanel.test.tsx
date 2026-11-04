import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MatterGovernancePanel } from './MatterGovernancePanel';

vi.mock('./Icon', () => ({ Icon: ({ name }: { name: string }) => <span data-testid={`icon-${name}`} /> }));

const s = new Proxy({} as Record<string, string>, { get: (_, key) => String(key) });

function renderPanel(overrides: Partial<React.ComponentProps<typeof MatterGovernancePanel>> = {}) {
  const props = {
    legalHold: false,
    legalHoldReason: null,
    legalHoldSetAt: null,
    retentionDays: null,
    onSetHold: vi.fn().mockResolvedValue(true),
    onSetRetention: vi.fn().mockResolvedValue(true),
    styles: s,
    ...overrides,
  };
  render(<MatterGovernancePanel {...props} />);
  return props;
}

describe('MatterGovernancePanel', () => {
  it('places a hold with the typed reason', async () => {
    const props = renderPanel();
    fireEvent.change(screen.getByLabelText('Hold reason'), { target: { value: 'Litigation hold' } });
    fireEvent.click(screen.getByText('Place hold'));
    await waitFor(() => expect(props.onSetHold).toHaveBeenCalledWith(true, 'Litigation hold'));
  });

  it('explains a hold and offers to lift it; retention is disabled meanwhile', async () => {
    const props = renderPanel({ legalHold: true, legalHoldReason: 'Subpoena', legalHoldSetAt: '2026-11-01T00:00:00Z' });
    expect(screen.getByText(/On hold since .*: Subpoena/)).toBeInTheDocument();
    expect(screen.getByLabelText('Retention days')).toBeDisabled();
    fireEvent.click(screen.getByText('Lift hold'));
    await waitFor(() => expect(props.onSetHold).toHaveBeenCalledWith(false, null));
  });

  it('applies a retention policy in days and clears it with an empty value', async () => {
    const props = renderPanel();
    fireEvent.change(screen.getByLabelText('Retention days'), { target: { value: '365' } });
    fireEvent.click(screen.getByText('Apply'));
    await waitFor(() => expect(props.onSetRetention).toHaveBeenCalledWith(365));

    fireEvent.change(screen.getByLabelText('Retention days'), { target: { value: '' } });
    fireEvent.click(screen.getByText('Keep everything'));
    await waitFor(() => expect(props.onSetRetention).toHaveBeenCalledWith(0));
  });

  it('describes an active policy', () => {
    renderPanel({ retentionDays: 90 });
    expect(screen.getByText(/removed once older than 90 days/)).toBeInTheDocument();
  });
});
