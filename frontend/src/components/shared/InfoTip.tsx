import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './Icon';
import s from './InfoTip.module.css';

interface InfoTipProps {
  /** Names the tip for screen readers, e.g. "About the chat model". */
  label: string;
  children: ReactNode;
  /** 'warning' marks a tip about a side effect worth knowing before acting. */
  tone?: 'info' | 'warning';
}

const MARGIN = 8;
const MAX_WIDTH = 300;

/**
 * A small (i) button that explains the setting beside it.
 *
 * Opens on hover or keyboard focus, and on tap (touch screens have no hover);
 * a tap elsewhere or Escape closes it. The text is rendered at the top of the
 * page and kept inside the viewport, so a scrolling dialog can't clip it and
 * it stays readable on a phone.
 */
export function InfoTip({ label, children, tone = 'info' }: InfoTipProps) {
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [position, setPosition] = useState<CSSProperties>({});
  const buttonRef = useRef<HTMLButtonElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  // Phones report a tap as hover + focus + click. Only a real mouse hovers and
  // only the keyboard focuses without a pointer, so a tap just toggles the tip.
  const pointerDown = useRef(false);
  const id = useId();
  const open = pinned || hovered;

  const place = useCallback(() => {
    const button = buttonRef.current;
    if (!button) return;
    const rect = button.getBoundingClientRect();
    const width = Math.min(MAX_WIDTH, window.innerWidth - MARGIN * 2);
    const left = Math.min(
      Math.max(rect.left + rect.width / 2 - width / 2, MARGIN),
      window.innerWidth - width - MARGIN,
    );
    // Measure at the final width: a narrower guess would misjudge the height
    // and the tip, placed above, could end up covering the button.
    if (tipRef.current) tipRef.current.style.width = `${width}px`;
    const height = tipRef.current?.offsetHeight ?? 0;
    // Below the button, or above it when there's no room below.
    const below = rect.bottom + 6;
    const top = height && below + height > window.innerHeight - MARGIN
      ? Math.max(rect.top - height - 6, MARGIN)
      : below;
    setPosition({ top, left, width });
  }, []);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const close = () => { setPinned(false); setHovered(false); };
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (!buttonRef.current?.contains(target) && !tipRef.current?.contains(target)) close();
    };
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open, place]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`${s.button} ${tone === 'warning' ? s.warning : ''}`}
        aria-label={label}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          pointerDown.current = false;
          setPinned((p) => !p);
        }}
        onPointerDown={() => { pointerDown.current = true; }}
        onPointerEnter={(e) => { if (e.pointerType === 'mouse') setHovered(true); }}
        onPointerLeave={(e) => { if (e.pointerType === 'mouse') setHovered(false); }}
        onFocus={() => { if (!pointerDown.current) setHovered(true); }}
        onBlur={() => { pointerDown.current = false; setHovered(false); }}
      >
        <Icon name={tone === 'warning' ? 'AlertTriangle' : 'Info'} size={13} />
      </button>
      {open && createPortal(
        <span
          ref={tipRef}
          id={id}
          role="tooltip"
          className={`${s.tip} ${tone === 'warning' ? s.tipWarning : ''}`}
          style={position}
        >
          {children}
        </span>,
        document.body,
      )}
    </>
  );
}
