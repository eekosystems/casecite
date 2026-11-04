import { useUIStore } from '../stores/uiStore';
import { useSettingsStore } from '../stores/settingsStore';
import { useResearch } from '../contexts/ResearchContext';
import {
  CitationModal,
  RAGSettingsModal,
  ShortcutsHelp,
  LoginModal,
  SignupModal,
  ForgotPasswordModal,
  ChangePasswordModal,
  MfaChallengeModal,
  FilePickerModal,
  ConnectorFolderPickerModal,
} from '../components';

export function AppModals() {
  const { showSettings, setShowSettings, showShortcutsHelp, setShowShortcutsHelp } = useUIStore();
  const { ragSettings, handleSaveSettings } = useSettingsStore();
  const { selectedCitation, setSelectedCitation } = useResearch();

  return (
    <>
      <RAGSettingsModal isOpen={showSettings} onClose={() => setShowSettings(false)} settings={ragSettings} onSave={handleSaveSettings} />
      <CitationModal isOpen={!!selectedCitation} onClose={() => setSelectedCitation(null)} citation={selectedCitation} />
      <ShortcutsHelp isOpen={showShortcutsHelp} onClose={() => setShowShortcutsHelp(false)} />

      <FilePickerModal />
      <ConnectorFolderPickerModal />

      <LoginModal />
      <SignupModal />
      <ForgotPasswordModal />
      <MfaChallengeModal />
      <ChangePasswordModal />
    </>
  );
}
