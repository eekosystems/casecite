import { useEffect, useRef } from 'react';
import s from './ReviewSourceText.module.css';

interface ReviewSourceTextProps {
  text: string;
  /** The span to highlight and scroll to; `exact` is false when it was found by search, not by offsets. */
  highlight: { start: number; end: number; exact: boolean } | null;
  /** Bumped to re-scroll to the same span. */
  seq?: number;
}

const CONTEXT_CHARS = 20_000;

/**
 * A document's text with one passage highlighted and scrolled into view. Very
 * long documents are trimmed to a window around the passage so the panel stays
 * responsive; the trimmed edges are marked.
 */
export function ReviewSourceText({ text, highlight, seq = 0 }: ReviewSourceTextProps) {
  const markRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    markRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [highlight?.start, highlight?.end, seq]);

  if (!highlight) {
    const shown = text.length > CONTEXT_CHARS ? text.slice(0, CONTEXT_CHARS) : text;
    return (
      <div className={s.doc}>
        {shown}
        {shown.length < text.length && <span className={s.trimmed}>… {text.length - shown.length} more characters</span>}
      </div>
    );
  }

  const from = Math.max(0, highlight.start - CONTEXT_CHARS / 2);
  const to = Math.min(text.length, highlight.end + CONTEXT_CHARS / 2);
  return (
    <div className={s.doc}>
      {from > 0 && <span className={s.trimmed}>… {from} characters before</span>}
      {text.slice(from, highlight.start)}
      <mark ref={markRef} className={`${s.mark} ${highlight.exact ? s.markExact : s.markApprox}`} data-testid="source-highlight">
        {text.slice(highlight.start, highlight.end)}
      </mark>
      {text.slice(highlight.end, to)}
      {to < text.length && <span className={s.trimmed}>… {text.length - to} characters after</span>}
    </div>
  );
}
