import { useMemo, useCallback, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useUIStore } from '../stores/uiStore';
import { useAuthStore } from '../stores/authStore';
import { useDocumentsStore } from '../stores/documentsStore';
import { useBrandingStore } from '../stores/brandingStore';
import { useToolsStore } from '../stores/toolsStore';
import { useApp } from '../contexts/AppContext';
import { ANALYSIS_MODES } from '../constants/analysisModes';
import { LEGAL_TOOLS } from '../constants/tools';
import { modeFromPath, MODE_TO_ROUTE } from '../routeConfig';
import { Icon, DocumentPanel } from '../components';
import type { UploadedDocument } from '../types';
import s from './LeftSidebar.module.css';

function LeftSidebar() {
  const {
    leftSidebarCollapsed, setLeftSidebarCollapsed,
    leftSidebarOpen, setLeftSidebarOpen, setRightPanelOpen,
  } = useUIStore();
  const { isAuthenticated, setShowSignupModal } = useAuthStore();
  const { documents, setDocuments } = useDocumentsStore();
  const branding = useBrandingStore((s) => s.branding);
  const openTool = useToolsStore((st) => st.openTool);
  const { fileInputRef } = useApp();

  const location = useLocation();
  const navigate = useNavigate();
  const activeMode = useMemo(() => modeFromPath(location.pathname), [location.pathname]);
  const activeToolId = useMemo(
    () => (location.pathname.startsWith('/tools/') ? location.pathname.slice('/tools/'.length) : null),
    [location.pathname],
  );
  const setActiveMode = useCallback((mode: string) => {
    navigate(MODE_TO_ROUTE[mode] || '/research');
  }, [navigate]);
  const analysisModes = ANALYSIS_MODES;

  const collapsed = leftSidebarCollapsed;

  // Under the tablet breakpoint the sidebars are slide-over drawers
  // (styles/responsive.css): both close on navigation, this one on Escape.
  useEffect(() => {
    setLeftSidebarOpen(false);
    setRightPanelOpen(false);
  }, [location.pathname, setLeftSidebarOpen, setRightPanelOpen]);

  useEffect(() => {
    if (!leftSidebarOpen) return;
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') setLeftSidebarOpen(false); };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [leftSidebarOpen, setLeftSidebarOpen]);

  return (
    <>
    <div className={`sidebar-overlay ${leftSidebarOpen ? 'active' : ''}`} onClick={() => setLeftSidebarOpen(false)} aria-hidden="true" />
    <aside id="left-sidebar" data-layout="left-sidebar" role="navigation" aria-label="Main navigation" className={`${s.sidebar} ${collapsed ? s.sidebarCollapsed : ''} ${leftSidebarOpen ? 'left-sidebar-open' : ''}`}>
      {/* Collapse Toggle */}
      <button
        onClick={() => setLeftSidebarCollapsed(!collapsed)}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className={`${s.collapseToggle} sidebar-collapse-btn`}
      >
        <Icon name={collapsed ? 'ChevronRight' : 'ChevronLeft'} size={16} className={s.iconGray400} />
      </button>

      <div className={`${s.sidebarInner} ${collapsed ? s.sidebarInnerCollapsed : ''}`}>
        {/* Logo */}
        <div className={`${s.logo} ${collapsed ? s.logoCollapsed : ''}`}>
          {branding.logo_url ? (
            <img src={branding.logo_url} alt={branding.firm_name || 'CaseCite'} className={s.logoImg} onError={e => { (e.target as HTMLImageElement).style.display = 'none'; }} />
          ) : (
            <span className={s.logoWordmark}>{collapsed ? 'C' : 'CaseCite'}<span className={s.logoDot}>.</span></span>
          )}
        </div>

        {/* Mode Selection */}
        <div className={s.modeSection}>
          {!collapsed && <div className={s.sectionLabel}>Analysis Mode</div>}
          <div className={`${s.modeList} ${collapsed ? s.modeListCollapsed : ''}`}>
            {analysisModes.map((mode: { id: string; title: string; icon: string; desc: string }) => (
              <button
                key={mode.id}
                className={`${s.modeBtn} ${activeMode === mode.id ? s.modeBtnActive : ''} ${collapsed ? s.modeBtnCollapsed : ''} ${!isAuthenticated ? s.modeBtnLocked : ''}`}
                onClick={() => {
                  if (!isAuthenticated) { setShowSignupModal(true); return; }
                  setActiveMode(mode.id);
                }}
                title={collapsed ? mode.title : undefined}
              >
                {!isAuthenticated && !collapsed && (
                  <Icon name="Lock" size={10} className={`${s.iconGold500} ${s.lockIconExpanded}`} />
                )}
                {!isAuthenticated && collapsed && (
                  <Icon name="Lock" size={8} className={`${s.iconGold500} ${s.lockIconCollapsed}`} />
                )}
                <div className={`${s.modeIcon} ${activeMode === mode.id ? s.modeIconActive : ''}`}>
                  <Icon name={mode.icon} size={16} />
                </div>
                {!collapsed && (
                  <div className={s.modeContent}>
                    <span className={s.modeTitle}>{mode.title}</span>
                    <span className={s.modeDesc}>{mode.desc}</span>
                  </div>
                )}
              </button>
            ))}
          </div>
        </div>

        {/* Legal Tools */}
        <div className={s.modeSection}>
          {!collapsed && <div className={s.sectionLabel}>Legal Tools</div>}
          <div className={`${s.modeList} ${collapsed ? s.modeListCollapsed : ''}`}>
            {LEGAL_TOOLS.map((tool) => {
              // Judge Intel is its own full workspace, not a ToolsView tool.
              const isJudgeIntel = tool.id === 'judge-analyzer';
              const isActive = isJudgeIntel
                ? location.pathname.startsWith('/judge-intel')
                : activeToolId === tool.id;
              return (
              <button
                key={tool.id}
                className={`${s.modeBtn} ${isActive ? s.modeBtnActive : ''} ${collapsed ? s.modeBtnCollapsed : ''} ${!isAuthenticated ? s.modeBtnLocked : ''}`}
                onClick={() => {
                  if (!isAuthenticated) { setShowSignupModal(true); return; }
                  if (isJudgeIntel) { navigate('/judge-intel'); return; }
                  openTool(tool);
                  navigate(`/tools/${tool.id}`);
                }}
                title={collapsed ? tool.name : undefined}
              >
                {!isAuthenticated && !collapsed && (
                  <Icon name="Lock" size={10} className={`${s.iconGold500} ${s.lockIconExpanded}`} />
                )}
                {!isAuthenticated && collapsed && (
                  <Icon name="Lock" size={8} className={`${s.iconGold500} ${s.lockIconCollapsed}`} />
                )}
                <div className={`${s.modeIcon} ${isActive ? s.modeIconActive : ''}`}>
                  <Icon name={tool.icon} size={16} />
                </div>
                {!collapsed && (
                  <div className={s.modeContent}>
                    <span className={s.modeTitle}>{tool.name}</span>
                    <span className={s.modeDesc}>{tool.desc}</span>
                  </div>
                )}
              </button>
              );
            })}          </div>
        </div>

        {/* Documents — Matters and the Knowledge Base sit above the upload box */}
        <div className={s.modeSection}>
          {!collapsed && <div className={s.sectionLabel}>Documents</div>}
          <div className={`${s.modeList} ${collapsed ? s.modeListCollapsed : ''}`}>
            <button
              className={`${s.modeBtn} ${activeMode === 'matters' ? s.modeBtnActive : ''} ${collapsed ? s.modeBtnCollapsed : ''} ${!isAuthenticated ? s.modeBtnLocked : ''}`}
              onClick={() => {
                if (!isAuthenticated) { setShowSignupModal(true); return; }
                setActiveMode('matters');
              }}
              title={collapsed ? 'Matters' : undefined}
            >
              {!isAuthenticated && !collapsed && (
                <Icon name="Lock" size={10} className={`${s.iconGold500} ${s.lockIconExpanded}`} />
              )}
              {!isAuthenticated && collapsed && (
                <Icon name="Lock" size={8} className={`${s.iconGold500} ${s.lockIconCollapsed}`} />
              )}
              <div className={`${s.modeIcon} ${activeMode === 'matters' ? s.modeIconActive : ''}`}>
                <Icon name="Briefcase" size={16} />
              </div>
              {!collapsed && (
                <div className={s.modeContent}>
                  <span className={s.modeTitle}>Matters</span>
                  <span className={s.modeDesc}>Client engagements &amp; sharing</span>
                </div>
              )}
            </button>
            <button
              className={`${s.modeBtn} ${activeMode === 'rag-docs' ? s.modeBtnActive : ''} ${collapsed ? s.modeBtnCollapsed : ''} ${!isAuthenticated ? s.modeBtnLocked : ''}`}
              onClick={() => {
                if (!isAuthenticated) { setShowSignupModal(true); return; }
                setActiveMode('rag-docs');
              }}
              title={collapsed ? 'Knowledge Base' : undefined}
            >
              {!isAuthenticated && !collapsed && (
                <Icon name="Lock" size={10} className={`${s.iconGold500} ${s.lockIconExpanded}`} />
              )}
              {!isAuthenticated && collapsed && (
                <Icon name="Lock" size={8} className={`${s.iconGold500} ${s.lockIconCollapsed}`} />
              )}
              <div className={`${s.modeIcon} ${activeMode === 'rag-docs' ? s.modeIconActive : ''}`}>
                <Icon name="Database" size={16} />
              </div>
              {!collapsed && (
                <div className={s.modeContent}>
                  <span className={s.modeTitle}>Knowledge Base</span>
                  <span className={s.modeDesc}>All documents &amp; sources</span>
                </div>
              )}
            </button>
          </div>
        </div>

        {/* Quick upload */}
        {!collapsed ? (
          <DocumentPanel
            documents={documents as unknown as UploadedDocument[]}
            onUpload={(doc: UploadedDocument) => setDocuments((prev) => [...prev, doc as unknown as Record<string, unknown>])}
            onRemove={(id: string) => setDocuments((prev) => prev.filter((d) => (d as unknown as UploadedDocument).id !== id))}
            onUpdateDoc={(id: string, updates: Partial<UploadedDocument>) => setDocuments((prev) => prev.map((d) => (d as unknown as UploadedDocument).id === id ? {...d, ...updates} : d))}
            inputRef={fileInputRef}
            isAuthenticated={isAuthenticated}
            onAuthRequired={() => setShowSignupModal(true)}
          />
        ) : (
          <div className={s.collapsedUploadSection}>
            <button
              onClick={() => isAuthenticated ? fileInputRef.current?.click() : setShowSignupModal(true)}
              className={`${s.collapsedUploadBtn} ${!isAuthenticated ? s.collapsedUploadBtnLocked : ''}`}
              title={isAuthenticated ? "Upload Documents" : "Register to upload documents"}
              aria-label="Upload documents"
            >
              <Icon name={isAuthenticated ? "Upload" : "Lock"} size={18} className={s.iconGray400} />
            </button>
            {documents.length > 0 && (
              <div className={s.collapsedDocCount}>{documents.length}</div>
            )}
          </div>
        )}

        {/* Import sources now live inside the Knowledge Base view (the "brain"),
            alongside all documents — open it via the Knowledge Base item above. */}

      </div>
    </aside>
    </>
  );
}

export { LeftSidebar };
export default LeftSidebar;
