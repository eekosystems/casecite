import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';

// Mock lazy-loaded views to avoid real imports
vi.mock('../views/ResearchView', () => ({ default: () => <div>ResearchView</div> }));
vi.mock('../views/RagDocsView', () => ({ default: () => <div>RagDocsView</div> }));
vi.mock('../views/JudgeIntelView', () => ({ default: () => <div>JudgeIntelView</div> }));
vi.mock('../views/CaseView', () => ({ default: () => <div>CaseView</div> }));
vi.mock('../views/ToolsView', () => ({ default: () => <div>ToolsView</div> }));
vi.mock('../views/ResetPasswordView', () => ({ default: () => <div>ResetPasswordView</div> }));
vi.mock('../views/NotFoundView', () => ({ default: () => <div>Page not found</div> }));
vi.mock('./AuthOverlay', () => ({ AuthOverlay: () => <div>AuthOverlay</div> }));

import { AppRoutes } from './AppRoutes';

// AppRoutes reads isAuthenticated from the auth store.
function renderRoute(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

describe('AppRoutes', () => {
  beforeEach(() => {
    useAuthStore.setState({ isAuthenticated: true });
  });

  it('shows AuthOverlay when not authenticated', () => {
    useAuthStore.setState({ isAuthenticated: false });
    renderRoute('/research');
    expect(screen.getByText('AuthOverlay')).toBeInTheDocument();
  });

  it('renders ResearchView on /research', async () => {
    renderRoute('/research');
    expect(await screen.findByText('ResearchView')).toBeInTheDocument();
  });

  it('redirects / to /research', async () => {
    renderRoute('/');
    expect(await screen.findByText('ResearchView')).toBeInTheDocument();
  });

  it('serves the emailed reset link while signed out', async () => {
    useAuthStore.setState({ isAuthenticated: false });
    renderRoute('/reset-password?token=abc');
    expect(await screen.findByText('ResetPasswordView')).toBeInTheDocument();
    expect(screen.queryByText('AuthOverlay')).toBeNull();
  });

  it('shows a not-found page for an unknown path instead of silently redirecting', async () => {
    renderRoute('/no-such-page');
    expect(await screen.findByText('Page not found')).toBeInTheDocument();
    expect(screen.queryByText('ResearchView')).toBeNull();
  });
});
