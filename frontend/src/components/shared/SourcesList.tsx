import { Icon } from './Icon';
import { activateOnKey } from './activateOnKey';
import { safeHttpUrl } from './safeUrl';
import { groupCitationsBySource, claimLabel, verificationBadge } from '../../utils';
import type { Citation } from '../../types';

interface SourcesListProps {
  styles: Record<string, string>;
  allCitations: Citation[];
  setSelectedCitation: (cite: Citation) => void;
  /** Hide the built-in heading when rendered inside a categorized section. */
  showTitle?: boolean;
  /** Citation currently hovered in the chat's numbered pills — spotlit here. */
  hoveredCitationId?: string | null;
}

function SourcesList({ styles, allCitations, setSelectedCitation, showTitle = true, hoveredCitationId }: SourcesListProps) {
  const groups = groupCitationsBySource(allCitations);
  return (
    <div className={styles.rightSection}>
      {showTitle && <h4 className={styles.rightSectionTitle}>Sources ({groups.length})</h4>}
      {groups.length === 0 ? (
        <div className={styles.emptyPanel}>
          <Icon name="FileSearch" size={24} className={styles.emptyPanelIcon} />
          <div>No sources yet</div>
          <div className={styles.emptyPanelDesc}>
            Run a search to see cited sources
          </div>
        </div>
      ) : (
        <div className={styles.sourcesList}>
          {groups.map(({ top: cite, count }, rowIndex) => {
            // Extract URL from notes if not in url field
            const sourceUrl = safeHttpUrl(cite.url || (cite.notes?.match(/https?:\/\/[^\s]+/)?.[0]));
            // A claim citation IS one fact of the answer — its row says the
            // fact; the filename is the meta line.
            const claim = claimLabel(cite);
            const badge = verificationBadge(cite);
            return (
              <div
                key={cite.id}
                className={`${styles.sourceItem} ${hoveredCitationId === cite.id ? styles.sourceItemHot : ''}`}
                role="button"
                tabIndex={0}
                onClick={() => setSelectedCitation(cite)}
                onKeyDown={activateOnKey(() => setSelectedCitation(cite))}
              >
                <span className={styles.sourceNum}>{rowIndex + 1}</span>
                <div className={styles.sourceContent}>
                  <span className={styles.sourceName}>{claim || cite.source}</span>
                  <span className={styles.sourceMeta}>
                    <span>{claim ? cite.source : (cite.type === 'case_law' ? 'Case Law' : 'Internal Doc')}</span>
                    {/* Quality signals are categorical: the explicit
                        verification state (what was and was not checked), and
                        a quiet flag when retrieval itself scored the passage
                        below the user's threshold. Never a numeric score. */}
                    {badge && (
                      <>
                        <span className={styles.sourceMetaRule} aria-hidden="true" />
                        <span className={styles[badge.className]} title={badge.title}>{badge.label}</span>
                      </>
                    )}
                    {cite.weakMatch && (
                      <>
                        <span className={styles.sourceMetaRule} aria-hidden="true" />
                        <span className={styles.weakMatch} title="Scored below your similarity threshold">Weak match</span>
                      </>
                    )}
                    {count > 1 && (
                      <>
                        <span className={styles.sourceMetaRule} aria-hidden="true" />
                        <span>{count} passages</span>
                      </>
                    )}
                    {cite.was_cited_by_ai && (
                      <>
                        <span className={styles.sourceMetaRule} aria-hidden="true" />
                        <span className={styles.sourceCited}>
                          <Icon name="Check" size={10} /> Cited
                        </span>
                      </>
                    )}
                  </span>
                </div>
                {sourceUrl && (
                  <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className={styles.sourceLink} onClick={e => e.stopPropagation()}>
                    <Icon name="ExternalLink" size={12} />
                  </a>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export { SourcesList };
