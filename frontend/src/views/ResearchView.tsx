import React, { useEffect, useState } from 'react';
import { useResearch } from '../contexts/ResearchContext';
import { useAuthStore } from '../stores/authStore';
import { useApp } from '../contexts/AppContext'; // fileInputRef only
import { ANALYSIS_MODES } from '../constants/analysisModes';
import { groupCitationsBySource, claimLabel, anchorToCitation } from '../utils';
import type { ChatMessage } from '../types';
import s from './ResearchView.module.css';
import { Icon, StrategyBriefCard, FloatingChatDock, MatterFileWorkspace, AnnotatedDocument, PdfDocumentViewer, AssistantAvatar, MessageMarkdown, AiNotice, DeepResearchPanel } from '../components';
import { useDeepResearchStore } from '../stores/deepResearchStore';

// Static constants only (no components, no rendering functions).
// Composer placeholder ticker: each phrase slides up into view in turn.
const COMPOSER_HINTS = [
  'Ask or strategize across your documents…',
  'Summarize the indemnification obligations across these agreements',
  'What are our strongest arguments in this matter?',
  'Give me all the case law for this file',
] as const;
const HINT_ROTATE_MS = 3500;

/** Real elapsed time for the in-flight request — the only progress we know. */
function ElapsedTime({ since }: { since: number | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (since == null) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [since]);
  if (since == null) return null;
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  const label = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return <span className={s.elapsed} aria-hidden="true">{label}</span>;
}

function ResearchView() {
  const {
    messages, isProcessing, processingStage, processingStartedAt,
    lastMessageRef, inputRef,
    inputValue, setInputValue,
    handleSend, handleCancel, handleClearSession, handleExportConversation, exportingConversation,
    setSelectedCitation,
    mainDocFilter, setMainDocFilter,
    openDocs, openDocument, closeDocument, docAnnotations,
    pendingMessageJump, pendingDocJump, setHoveredCitationId,
  } = useResearch();
  const { isAuthenticated, setShowSignupModal } = useAuthStore();
  const { fileInputRef } = useApp();
  const analysisModes = ANALYSIS_MODES;
  const deepResearchOpen = useDeepResearchStore((st) => st.open);
  const toggleDeepResearch = useDeepResearchStore((st) => st.toggle);

  // Local UI state only: the floating conversation dock and composer collapse.
  const [chatOpen, setChatOpen] = useState(true);
  const [composerCollapsed, setComposerCollapsed] = useState(false);
  useEffect(() => {
    if (messages.length > 0) setChatOpen(true);
  }, [messages.length]);

  // Sources panel jump: open the dock and scroll the conversation to the
  // answer that produced the clicked group of sources.
  useEffect(() => {
    if (!pendingMessageJump) return;
    setChatOpen(true);
    requestAnimationFrame(() => {
      document.getElementById(`chat-msg-${pendingMessageJump.id}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }, [pendingMessageJump]);

  // Full-screen document view: entered when a file is opened, left via the
  // back button; open documents survive going back to the file list.
  const [docScreen, setDocScreen] = useState(false);
  useEffect(() => {
    if (openDocs.length === 0) setDocScreen(false);
  }, [openDocs.length]);
  // Per-pane view mode: native document render (real formatting) vs extracted
  // text with case-law anchors. Defaults to the native view when it exists.
  const [paneModes, setPaneModes] = useState<Record<string, 'doc' | 'text'>>({});

  // Citation tracing: the modal asked to land on a span — enter the document
  // screen with that file in text view; AnnotatedDocument scrolls + flashes.
  useEffect(() => {
    if (!pendingDocJump) return;
    setDocScreen(true);
    setPaneModes((prev) => ({ ...prev, [pendingDocJump.docId]: 'text' }));
  }, [pendingDocJump]);

  // Rotate the composer's placeholder through the inspiration hints.
  const [hintIndex, setHintIndex] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setHintIndex((i) => (i + 1) % COMPOSER_HINTS.length), HINT_ROTATE_MS);
    return () => clearInterval(timer);
  }, []);

  return (
          <div className={s.chatArea}>
            <div className={s.messagesWrap}>
              {docScreen && openDocs.length > 0 ? (
                /* Full-screen reading view: 1 doc = full width, 2 = 50/50,
                   3-4 = quartered. The conversation's verified claims are
                   anchored inline — click a highlight for the verification popup. */
                <div className={s.docScreen}>
                  <div className={s.docScreenBar}>
                    <button className={s.docBackBtn} onClick={() => setDocScreen(false)}>
                      <Icon name="ArrowLeft" size={14} /> Files
                    </button>
                    <span className={s.docScreenTitle}>
                      {openDocs.length === 1
                        ? openDocs[0].filename
                        : `${openDocs.length} documents open`}
                    </span>
                  </div>
                  <div className={`${s.docGrid} ${openDocs.length > 1 ? s.docGridSplit : ''}`}>
                    {openDocs.map((d) => {
                      const anchorCount = docAnnotations[d.id]?.length ?? 0;
                      const mode = d.fileUrl ? (paneModes[d.id] ?? 'doc') : 'text';
                      return (
                      <div key={d.id} className={s.docPane}>
                        <div className={s.docPaneHeader}>
                          <Icon name="FileText" size={13} />
                          <span className={s.docPaneName}>{d.filename}</span>
                          {d.fileUrl && (
                            <span className={s.paneToggle}>
                              <button
                                className={mode === 'doc' ? s.paneToggleActive : s.paneToggleBtn}
                                onClick={() => setPaneModes((prev) => ({ ...prev, [d.id]: 'doc' }))}
                              >
                                Document
                              </button>
                              <button
                                className={mode === 'text' ? s.paneToggleActive : s.paneToggleBtn}
                                onClick={() => setPaneModes((prev) => ({ ...prev, [d.id]: 'text' }))}
                              >
                                Text{anchorCount > 0 ? ` · ${anchorCount} anchor${anchorCount === 1 ? '' : 's'}` : ''}
                              </button>
                            </span>
                          )}
                          <button
                            className={s.docPaneClose}
                            onClick={() => closeDocument(d.id)}
                            aria-label={`Close ${d.filename}`}
                          >
                            <Icon name="X" size={12} />
                          </button>
                        </div>
                        <div className={mode === 'doc' && !d.loading ? s.docPaneBodyFlush : s.docPaneBody}>
                          {d.loading ? (
                            <div className={s.docLoading}>
                              <Icon name="Loader2" size={20} className={s.spinnerIcon} />
                              <span>Opening {d.filename}…</span>
                            </div>
                          ) : mode === 'doc' && d.fileUrl ? (
                            <PdfDocumentViewer fileUrl={d.fileUrl} />
                          ) : (
                            <AnnotatedDocument
                              text={d.text}
                              annotations={docAnnotations[d.id] || []}
                              onVerify={(m) => setSelectedCitation(anchorToCitation(m, 0, `doc-${d.id}`))}
                              jumpToSpan={pendingDocJump?.docId === d.id
                                ? { start: pendingDocJump.start, seq: pendingDocJump.seq }
                                : null}
                            />
                          )}
                        </div>
                      </div>
                      );
                    })}
                  </div>
                </div>
              ) : (
                /* The workspace IS the page: your matters, each with its files.
                   Matter click = chat with that matter; tick several to chat
                   with all of them; file click = chat with that file. The
                   conversation floats above it. */
                <div className={s.workspaceWrap}>
                  <div className={s.workspaceHead}>
                    <h3 className={s.workspaceTitle}>Matter Strategy</h3>
                    <p className={s.workspaceSub}>
                      Pick a matter, several matters or individual files to scope the chat, or chat
                      across all of them — ask questions, or get a strategy brief with verified case
                      law for every point (&ldquo;give me all the case law for this matter&rdquo;).
                    </p>
                  </div>
                  {openDocs.length > 0 && (
                    <button className={s.docReturnBtn} onClick={() => setDocScreen(true)}>
                      <Icon name="BookOpen" size={13} />
                      Back to open document{openDocs.length === 1 ? '' : 's'} ({openDocs.length})
                    </button>
                  )}
                  <MatterFileWorkspace
                    currentFilter={mainDocFilter}
                    onScopeChange={setMainDocFilter}
                    onOpenFile={(id, name) => { openDocument(id, name); setDocScreen(true); }}
                  />
                </div>
              )}
            </div>

            {/* Floating conversation — same dock as Contracts; the page behind
                stays clean and everything can minimize out of the way. */}
            {(messages.length > 0 || isProcessing) && (
            <FloatingChatDock
              open={chatOpen}
              low={composerCollapsed}
              busy={isProcessing}
              count={messages.length}
              onMinimize={() => setChatOpen(false)}
              onExpand={() => setChatOpen(true)}
              onClear={handleClearSession}
              onExport={handleExportConversation}
              exporting={exportingConversation}
            >
              {messages.map((msg: ChatMessage, msgIndex: number) => (
                <div
                  key={msg.id}
                  id={`chat-msg-${msg.id}`}
                  ref={msgIndex === messages.length - 1 ? lastMessageRef : undefined}
                  className={msg.type === 'user' ? s.messageRowUser : s.messageRowAssistant}
                >
                  {msg.type === 'assistant' && (
                    <div className={s.assistantAvatar}><AssistantAvatar /></div>
                  )}
                  <div className={msg.type === 'user' ? s.userBubble : s.assistantBubble}>
                    {msg.type === 'user' ? (
                      <div className={s.userMessageContent}>{msg.content}</div>
                    ) : (
                      <>
                    {msg.stats && (
                      <div className={s.statsBar}>
                        <span><Icon name="Search" size={12} /> {msg.stats.docsSearched.toLocaleString()} docs</span>
                        <span><Icon name="Layers" size={12} /> {msg.stats.chunksRetrieved} chunks</span>
                        {(msg.stats.caseLawIncluded ?? 0) > 0 && (
                          <span className={s.caseLawStat}><Icon name="Scale" size={12} /> {msg.stats.caseLawIncluded} cases</span>
                        )}
                        <span><Icon name="Zap" size={12} /> {msg.stats.processingTime}</span>
                        <span className={s.modeBadge}>{analysisModes.find((m: { id: string; title: string }) => m.id === msg.mode)?.title || 'Research'}</span>
                      </div>
                    )}
                    {msg.strategy && (
                      <StrategyBriefCard
                        brief={msg.strategy}
                        citations={msg.citations || []}
                        onCitationClick={setSelectedCitation}
                        styles={s}
                      />
                    )}                    {!msg.strategy && (
                      <MessageMarkdown text={msg.content} />
                    )}
                    {msg.citations && msg.citations.length > 0 && (
                      <div className={s.citationsBlock}>
                        <div className={s.citationsHeader}>
                          <Icon name="BookOpen" size={14} />
                          <span>Sources ({groupCitationsBySource(msg.citations).length})</span>
                          <span className={s.citationsHint}>Click to review</span>
                        </div>
                        <div className={s.citationPills}>
                          {/* Compact numbered pills — the full rows live in the
                              Sources panel; hovering a pill spotlights its row
                              there, clicking opens the verification popup. */}
                          {groupCitationsBySource(msg.citations).map(({ top: cite }, pillIndex) => (
                            <button
                              key={cite.id}
                              className={cite.verified === false ? s.citationPillUnverified : s.citationPill}
                              title={claimLabel(cite) || cite.source}
                              onClick={() => setSelectedCitation(cite)}
                              onMouseEnter={() => setHoveredCitationId(cite.id)}
                              onMouseLeave={() => setHoveredCitationId(null)}
                            >
                              {pillIndex + 1}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                    {/* The brief card carries its own notice */}
                    {!msg.strategy && <AiNotice />}
                    <span className={s.timestamp}>{msg.timestamp.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}</span>
                      </>
                    )}
                  </div>
                </div>
              ))}

            {isProcessing && (
              <div className={s.messageRow}>
                <div className={s.assistantAvatar}><AssistantAvatar /></div>
                <div className={s.processingBubble}>
                  <div className={s.processingContent}>
                    <Icon name="Loader2" size={16} className={s.spinnerIcon} />
                    <span>{processingStage}</span>
                    <ElapsedTime since={processingStartedAt} />
                    <button type="button" className={s.cancelBtn} onClick={handleCancel}>
                      Cancel
                    </button>
                  </div>
                  <div className={s.progressBar}>
                    <div className={s.progressFill} />
                  </div>
                </div>
              </div>
            )}

            </FloatingChatDock>
            )}

            {/* Full-page view: the whole composer collapses to a floating pill */}
            {composerCollapsed && (
              <button className={s.composerToggle} onClick={() => setComposerCollapsed(false)}>
                <Icon name="MessageSquarePlus" size={13} /> Composer
              </button>
            )}

            {/* Input */}
            {!composerCollapsed && (
          <div className={s.inputSection}>
            {deepResearchOpen && isAuthenticated && <DeepResearchPanel docFilter={mainDocFilter} />}
            <div className={s.inputWrap}>
              {/* Lock overlay when not authenticated */}
              {!isAuthenticated && (
                <div
                  onClick={() => setShowSignupModal(true)}
                  className={s.lockOverlay}
                >
                  <Icon name="Lock" size={20} className={s.lockIcon} />
                  <span className={s.lockText}>Create an account to search</span>
                  <span className={s.lockCta}>Register</span>
                </div>
              )}
              <button className={s.attachBtn} onClick={() => isAuthenticated ? fileInputRef.current?.click() : setShowSignupModal(true)} title="Attach documents"><Icon name="Paperclip" size={18} /></button>
              <div className={s.inputTicker}>
                <input
                  aria-label="Ask or strategize across your documents"
                  ref={inputRef as unknown as React.RefObject<HTMLInputElement>}
                  type="text"
                  value={inputValue}
                  onChange={e => setInputValue(e.target.value)}
                  className={s.input}
                  disabled={isProcessing || !isAuthenticated}
                />
                {!inputValue && (
                  <span key={hintIndex} className={s.inputTickerHint} aria-hidden="true">
                    {COMPOSER_HINTS[hintIndex]}
                  </span>
                )}
              </div>
              <button className={`${s.sendBtn} ${!inputValue.trim() || isProcessing ? s.sendBtnDisabled : ''}`} onClick={handleSend} disabled={!inputValue.trim() || isProcessing} aria-label="Send question">
                <Icon name="Search" size={18} />
              </button>
            </div>
            <div className={s.inputHints}>
              {isAuthenticated ? (
                <>
                  <span>Press <kbd className={s.kbd}>Enter</kbd> to search</span>
                  <span>{'\u2022'}</span>
                  <span><kbd className={s.kbd}>{'\u2318'}K</kbd> to focus</span>
                  <span>{'\u2022'}</span>
                  <button
                    type="button"
                    className={`${s.deepResearchBtn} ${deepResearchOpen ? s.deepResearchBtnActive : ''}`}
                    onClick={toggleDeepResearch}
                    aria-pressed={deepResearchOpen}
                    title="Plan a multi-step research run with verified sources"
                  >
                    <Icon name="ScanSearch" size={11} /> Deep research
                  </button>
                </>
              ) : (
                <span className={s.unauthHint}>Create a free account to access all features</span>
              )}
              <button
                className={s.composerCollapseBtn}
                onClick={() => setComposerCollapsed(true)}
                title="Hide the composer for a full-page view"
              >
                <Icon name="ChevronsDown" size={12} /> Hide
              </button>
            </div>
          </div>
            )}
        </div>
  );
}

export default ResearchView;
