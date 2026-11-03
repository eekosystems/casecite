import { useMemo, useCallback, useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import s from './App.module.css';

// Zustand stores (only those needed at App level for hooks/effects/callbacks)
import { useUIStore } from './stores/uiStore';
import { useAuthStore } from './stores/authStore';
import { useSettingsStore } from './stores/settingsStore';
import { useConnectorsStore } from './stores/connectorsStore';
import { useRagDocsStore } from './stores/ragDocsStore';

// Shared navigate reference for stores
import { setNavigate } from './utils/router';

// Hooks (feature-specific — still using React hooks due to DOM refs)
import { useResearchState } from './hooks/useResearchState';
import { useAppUIState } from './hooks/useAppUIState';
import { useAppEffects } from './hooks/useAppEffects';

// Route config
import { modeFromPath, MODE_TO_ROUTE } from './routeConfig';

// Layout & components
import { ErrorBoundary, ConfirmModal } from './components';
import { LeftSidebar, RightPanel, AppModals, AppHeader, ToastContainer, AppRoutes } from './layout';
import AppContext from './contexts/AppContext';
import ResearchContext from './contexts/ResearchContext';

const App = () => {
  // Router
  const location = useLocation();
  const navigate = useNavigate();
  const activeMode = useMemo(() => modeFromPath(location.pathname), [location.pathname]);
  const setActiveMode = useCallback((mode: string) => {
    navigate(MODE_TO_ROUTE[mode] || '/research');
  }, [navigate]);

  // Set shared navigate reference for stores (once)
  useEffect(() => { setNavigate(navigate); }, [navigate]);

  // Zustand stores (only what hooks/effects/callbacks need)
  const { confirmModal, closeConfirm, handleConfirm } = useUIStore();
  const addToast = useUIStore(st => st.addToast);
  const authState = useAuthStore();
  const appSettingsState = useSettingsStore();
  const connectorsState = useConnectorsStore();
  const ragDocsState = useRagDocsStore();

  // Load RAG docs when authenticated
  useEffect(() => {
    if (authState.isAuthenticated) ragDocsState.loadRagDocuments();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-run only on auth flips; loader identity is unstable
  }, [authState.isAuthenticated]);

  // Research (before AppUI — AppUI needs setSelectedCitation & inputRef from research)
  const researchState = useResearchState({
    addToast, activeMode, setActiveMode,
  });

  // App UI (keyboard shortcuts, file ref)
  const appUIState = useAppUIState({
    setSelectedCitation: researchState.setSelectedCitation,
    setShowSignupModal: authState.setShowSignupModal,
    setShowLoginModal: authState.setShowLoginModal,
    setShowPickerModal: connectorsState.setShowPickerModal,
    setActiveMode, inputRef: researchState.inputRef,
  });

  // App-level effects (initial load, persistence, key bindings)
  useAppEffects({
    setUser: authState.setUser, setIsAuthenticated: authState.setIsAuthenticated,
    loadConnectors: connectorsState.loadConnectors,
    setSystemStats: appSettingsState.setSystemStats,
    messages: researchState.messages, lastMessageRef: researchState.lastMessageRef,
    ragSettings: appSettingsState.ragSettings,
    inputRef: researchState.inputRef, handleSend: researchState.handleSend,
  });

  // Minimal AppContext value — only provides fileInputRef
  const appContextValue = useMemo(() => ({
    fileInputRef: appUIState.fileInputRef,
  }), [appUIState.fileInputRef]);

  return (
    <ErrorBoundary>
    <AppContext.Provider value={appContextValue}>
    <ResearchContext.Provider value={researchState}>
    <div className={s.app} data-layout="app">
      <AppModals />
      <ToastContainer />
      <LeftSidebar />
      <main id="main-content" data-layout="main-content" className={s.main} role="main">
        <AppHeader />
        <AppRoutes />
      </main>
      <RightPanel />
    </div>
    </ResearchContext.Provider>
    </AppContext.Provider>
    <ConfirmModal
      isOpen={confirmModal.open}
      onClose={closeConfirm}
      onConfirm={handleConfirm}
      title={confirmModal.title}
      message={confirmModal.message}
      type={confirmModal.type as 'info' | 'warning' | 'danger'}
      confirmText={confirmModal.confirmText}
      cancelText={confirmModal.cancelText}
    />
    </ErrorBoundary>
  );
};

export { App };
