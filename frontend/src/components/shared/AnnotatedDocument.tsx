import React, { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import type { DocAnchor } from '../../api/types';
import s from './AnnotatedDocument.module.css';

export interface DocAnnotation {
  start: number;
  end: number;
  mappings: DocAnchor[];
}

interface AnnotatedDocumentProps {
  text: string;
  annotations: DocAnnotation[];
  onVerify: (m: DocAnchor) => void;
  /** Scroll to and flash the annotation at this char offset; seq retriggers. */
  jumpToSpan?: { start: number; seq: number } | null;
}

export function AnnotatedDocument({ text, annotations, onVerify, jumpToSpan }: AnnotatedDocumentProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [flashStart, setFlashStart] = useState<number | null>(null);

  // Citation tracing: land on the exact highlighted span and flash it.
  useEffect(() => {
    if (!jumpToSpan) return;
    const el = containerRef.current?.querySelector(`[data-ann-start="${jumpToSpan.start}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setFlashStart(jumpToSpan.start);
    const timer = setTimeout(() => setFlashStart(null), 2000);
    return () => clearTimeout(timer);
  }, [jumpToSpan]);
  // Sort by start, drop overlapping/invalid ranges (keep the first).
  const sorted = annotations
    .filter((a) => a.start >= 0 && a.end > a.start && a.end <= text.length)
    .sort((a, b) => a.start - b.start);
  const clean: DocAnnotation[] = [];
  let lastEnd = -1;
  for (const a of sorted) {
    if (a.start >= lastEnd) {
      clean.push(a);
      lastEnd = a.end;
    }
  }

  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  clean.forEach((a, i) => {
    if (a.start > cursor) nodes.push(<span key={`t${i}`}>{text.slice(cursor, a.start)}</span>);
    const verified = a.mappings.some((m) => m.verified);
    nodes.push(
      <span
        key={`h${i}`}
        data-ann-start={a.start}
        className={`${s.mark} ${verified ? s.markVerified : s.markUnverified} ${flashStart === a.start ? s.markFlash : ''}`}
      >
        {text.slice(a.start, a.end)}
        <span className={s.popover} role="tooltip">
          {a.mappings.map((m, mi) => (
            <span key={mi} className={s.cite}>
              <span className={s.citeTop}>
                <span className={m.verified ? s.badgeV : s.badgeU}>
                  <Icon name={m.verified ? 'ShieldCheck' : 'ShieldAlert'} size={11} />
                  {m.verified ? 'Verified' : 'Unverified'}
                </span>
                <span className={s.src}>{m.source === 'internal' ? 'Your documents' : 'CourtListener'}</span>
              </span>
              <span className={s.citeName}>{m.case_name || m.citation || 'Authority'}</span>
              {m.citation && <span className={s.citeRef}>{m.citation}</span>}
              {m.support_quote && <span className={s.citeQuote}>“{m.support_quote}”</span>}
              <button className={s.openBtn} onClick={() => onVerify(m)}>
                <Icon name="ScanSearch" size={11} /> Verify citation
              </button>
            </span>
          ))}
        </span>
      </span>,
    );
    cursor = a.end;
  });
  if (cursor < text.length) nodes.push(<span key="tail">{text.slice(cursor)}</span>);

  return <div ref={containerRef} className={s.doc}>{nodes}</div>;
}
