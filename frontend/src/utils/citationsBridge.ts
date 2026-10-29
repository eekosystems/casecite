/**
 * Shared citations sink for Zustand stores.
 *
 * Citations live in React state inside useResearchState (the Sources panel).
 * The hook registers its merge handler here — the same pattern as the shared
 * navigate reference in utils/router.ts — so stores can surface citations into the Sources panel without touching React state.
 */
import type { Citation } from '../types/research';

let _sink: ((citations: Citation[]) => void) | null = null;

export const setCitationsSink = (fn: ((citations: Citation[]) => void) | null) => { _sink = fn; };
export const pushCitations = (citations: Citation[]) => { _sink?.(citations); };
