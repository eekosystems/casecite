import { useAuthStore } from '../stores/authStore';
import { useBrandingStore } from '../stores/brandingStore';
import { Icon } from '../components';
import s from './AuthOverlay.module.css';

export function AuthOverlay() {
  const { setShowSignupModal, setShowLoginModal } = useAuthStore();
  const demoLogin = useBrandingStore(state => state.branding.demo_login);

  return (
    <div className={s.overlay}>
      <div className={s.iconCircle}>
        <Icon name="Lock" size={36} className={s.iconGold} />
      </div>
      <h2 className={s.heading}>
        Welcome to CaseCite
      </h2>
      <p className={s.subtext}>
        CaseCite is a legal research platform. Upload your documents, ask questions
        across them, and research verified case law and judges — all on
        infrastructure you control, using your own AI keys.
      </p>
      {demoLogin && (
        <p className={s.demoNote}>
          This is a public demo. Click <strong>Sign In</strong> — the demo account
          ({demoLogin.email}) is already filled in.
        </p>
      )}
      <div className={s.buttonGroup}>
        <button
          onClick={() => setShowSignupModal(true)}
          className={s.primaryButton}
        >
          <Icon name="UserPlus" size={18} />
          Register
        </button>
        <button
          onClick={() => setShowLoginModal(true)}
          className={s.secondaryButton}
        >
          Sign In
        </button>
      </div>
    </div>
  );
}
