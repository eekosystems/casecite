import { useEffect, useId } from 'react';
import { useAuthStore } from '../../stores/authStore';
import { useBrandingStore } from '../../stores/brandingStore';
import s from './AuthModals.module.css';
import { Icon } from '../shared/Icon';
import { ModalShell } from '../shared/ModalShell';

function LoginModal() {
  const id = useId();
  const {
    showLoginModal, setShowLoginModal,
    loginEmail, setLoginEmail, loginPassword, setLoginPassword,
    loginError, loginLoading,
    handleLoginSubmit, handleShowSignup,
    setShowForgotPassword, setForgotEmail, setForgotSuccess,
  } = useAuthStore();
  const demoLogin = useBrandingStore(state => state.branding.demo_login);

  // Public demo instance: prefill the shared account when the form opens
  // empty, so a visitor only has to click Sign In. Anything already typed
  // (or a prefilled email after a password change) is left alone.
  useEffect(() => {
    if (!showLoginModal || !demoLogin) return;
    const current = useAuthStore.getState();
    if (current.loginEmail || current.loginPassword) return;
    current.setLoginEmail(demoLogin.email);
    current.setLoginPassword(demoLogin.password);
  }, [showLoginModal, demoLogin]);

  if (!showLoginModal) return null;

  return (
    <ModalShell onClose={() => setShowLoginModal(false)} overlayClassName={s.modalOverlay} className={s.modalContainerSmall} labelledBy={`${id}-title`}>
        <div className={s.modalHeader}>
          <div className={s.modalHeaderLeft}>
            <div className={s.modalIconNavy}>
              <Icon name="LogIn" size={20} style={{color: 'var(--on-secondary)'}} />
            </div>
            <div>
              <h2 id={`${id}-title`} className={s.modalTitle}>Sign In</h2>
              <p className={s.modalSubtitle}>Welcome back</p>
            </div>
          </div>
          <button className={s.modalClose} onClick={() => setShowLoginModal(false)} aria-label="Close"><Icon name="X" size={20} /></button>
        </div>
        <div className={s.modalBody}>
          {demoLogin && (
            <div className={s.infoBox} role="note">
              This is a public demo. The shared demo account is filled in — just click Sign In.
            </div>
          )}
          {loginError && (
            <div className={s.errorBox}>
              {loginError}
            </div>
          )}
          <form id={`${id}-form`} onSubmit={e => { e.preventDefault(); handleLoginSubmit(); }} className={s.formLayout}>
            <div>
              <label htmlFor={`${id}-email`} className={s.formLabel}>Email</label>
              <input id={`${id}-email`} type="email" className={s.modalInput} value={loginEmail} onChange={e => setLoginEmail(e.target.value)}
                placeholder="you@example.com" autoComplete="email" />
            </div>
            <div>
              <label htmlFor={`${id}-password`} className={s.formLabel}>Password</label>
              <input id={`${id}-password`} type="password" className={s.modalInput} value={loginPassword} onChange={e => setLoginPassword(e.target.value)}
                placeholder="Your password" autoComplete="current-password" />
            </div>
            <button
              type="button"
              onClick={() => { setShowLoginModal(false); setShowForgotPassword(true); setForgotEmail(loginEmail); setForgotSuccess(false); }}
              className={s.forgotLink}
            >
              Forgot password?
            </button>
          </form>
        </div>
        <div className={s.modalFooter}>
          <button type="button" className={s.btnSecondary} onClick={() => setShowLoginModal(false)}>Cancel</button>
          {/* A real submit button tied to the form, so Enter in either field signs in. */}
          <button type="submit" form={`${id}-form`} className={s.btnPrimary} disabled={loginLoading}>
            {loginLoading ? 'Signing in...' : 'Sign In'}
          </button>
        </div>
        <div className={s.bottomBar}>
          Don't have an account?{' '}
          <button type="button" onClick={handleShowSignup} className={s.switchLink}>
            Register
          </button>
        </div>
    </ModalShell>
  );
}

export { LoginModal };
