import { describe, it, expect } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import { InfoTip } from './InfoTip';

function renderTip(tone?: 'info' | 'warning') {
  return render(
    <div>
      <InfoTip label="About the chat model" tone={tone}>Answers questions in chat.</InfoTip>
      <button type="button">elsewhere</button>
    </div>,
  );
}

describe('InfoTip', () => {
  it('is a labelled button with the explanation hidden until asked', () => {
    renderTip();
    const button = screen.getByRole('button', { name: 'About the chat model' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('opens on tap and closes on a tap elsewhere (touch screens have no hover)', () => {
    renderTip();
    const button = screen.getByRole('button', { name: 'About the chat model' });
    fireEvent.click(button);
    const tip = screen.getByRole('tooltip');
    expect(tip.textContent).toBe('Answers questions in chat.');
    expect(button.getAttribute('aria-describedby')).toBe(tip.id);

    fireEvent.pointerDown(screen.getByRole('button', { name: 'elsewhere' }));
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('opens on hover and keyboard focus, and closes with Escape', () => {
    renderTip();
    const button = screen.getByRole('button', { name: 'About the chat model' });
    fireEvent.pointerEnter(button, { pointerType: 'mouse' });
    expect(screen.getByRole('tooltip')).toBeTruthy();
    fireEvent.pointerLeave(button, { pointerType: 'mouse' });
    expect(screen.queryByRole('tooltip')).toBeNull();

    fireEvent.focus(button);
    expect(screen.getByRole('tooltip')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('a tap toggles it: the hover and focus a phone reports with a tap are ignored', () => {
    renderTip();
    const button = screen.getByRole('button', { name: 'About the chat model' });
    // A tap: touch pointer enters, presses, focuses, clicks.
    fireEvent.pointerEnter(button, { pointerType: 'touch' });
    fireEvent.pointerDown(button, { pointerType: 'touch' });
    fireEvent.focus(button);
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.click(button);
    expect(screen.getByRole('tooltip')).toBeTruthy();
    // A second tap closes it (focus is still on the button).
    fireEvent.pointerDown(button, { pointerType: 'touch' });
    fireEvent.click(button);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('renders outside the dialog so a scrolling container cannot clip it', () => {
    const { container } = renderTip();
    fireEvent.click(screen.getByRole('button', { name: 'About the chat model' }));
    const tip = screen.getByRole('tooltip');
    expect(container.contains(tip)).toBe(false);
    expect(document.body.contains(tip)).toBe(true);
  });

  it('tapping inside a label does not activate the labelled control', () => {
    let clicked = 0;
    render(
      <label>
        Setting <InfoTip label="About the setting">Text</InfoTip>
        <input type="checkbox" onClick={() => { clicked += 1; }} />
      </label>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'About the setting' }));
    expect(clicked).toBe(0);
  });
});
