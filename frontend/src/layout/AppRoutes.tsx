import React, { Suspense } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { ErrorBoundary } from '../components/shared/ErrorBoundary';
import { AuthOverlay } from './AuthOverlay';
import s from './AppRoutes.module.css';

// Code-split view imports
const RagDocsView = React.lazy(() => import('../views/RagDocsView'));
const JudgeIntelView = React.lazy(() => import('../views/JudgeIntelView'));
const CaseView = React.lazy(() => import('../views/CaseView'));
const ResearchView = React.lazy(() => import('../views/ResearchView'));
const ToolsView = React.lazy(() => import('../views/ToolsView'));
const ContractsView = React.lazy(() => import('../views/ContractsView'));
const DraftingView = React.lazy(() => import('../views/DraftingView'));
const MattersView = React.lazy(() => import('../views/MattersView'));
const MatterWorkspaceView = React.lazy(() => import('../views/MatterWorkspaceView'));
const ReviewTablesView = React.lazy(() => import('../views/ReviewTablesView'));
const ReviewTableView = React.lazy(() => import('../views/ReviewTableView'));
const WorkflowsView = React.lazy(() => import('../views/WorkflowsView'));
const WorkflowView = React.lazy(() => import('../views/WorkflowView'));
const ResetPasswordView = React.lazy(() => import('../views/ResetPasswordView'));
const DeviceApprovalView = React.lazy(() => import('../views/DeviceApprovalView'));
const NotFoundView = React.lazy(() => import('../views/NotFoundView'));

/** The emailed reset link must work signed out — it is how you get back in. */
const RESET_PASSWORD_PATH = '/reset-password';

function ViewLoadingFallback() {
  return (
    <div className={s.fallbackContainer}>
      <div className={s.fallbackContent}>
        <div className={s.spinner} />
        <div className={s.loadingText}>Loading...</div>
      </div>
    </div>
  );
}

export function AppRoutes() {
  const isAuthenticated = useAuthStore(s => s.isAuthenticated);
  const { pathname } = useLocation();

  if (pathname === RESET_PASSWORD_PATH) {
    return (
      <Suspense fallback={<ViewLoadingFallback />}>
        <ResetPasswordView />
      </Suspense>
    );
  }

  if (!isAuthenticated) {
    return <AuthOverlay />;
  }

  // A view that throws must not blank the whole shell: this boundary keeps
  // the sidebars and header alive, and re-keying it on the path lets the
  // user navigate to another view without a reload.
  return (
    <ErrorBoundary
      key={pathname}
      variant="inline"
      message="This view hit an unexpected error. The rest of the app still works — switch views, or reload to try again."
    >
    <Suspense fallback={<ViewLoadingFallback />}>
      <Routes>
        <Route path="/research" element={<ResearchView />} />
        <Route path="/documents" element={<RagDocsView />} />
        <Route path="/judge-intel" element={<JudgeIntelView />} />
        <Route path="/case/:id" element={<CaseView />} />
        <Route path="/case" element={<CaseView />} />
        <Route path="/tools/:toolId" element={<ToolsView />} />
        <Route path="/tools" element={<ToolsView />} />
        <Route path="/contracts" element={<ContractsView />} />
        <Route path="/drafting" element={<DraftingView />} />
        <Route path="/matters/:id" element={<MatterWorkspaceView />} />
        <Route path="/matters" element={<MattersView />} />
        <Route path="/review-tables/:id" element={<ReviewTableView />} />
        <Route path="/review-tables" element={<ReviewTablesView />} />
        <Route path="/workflows/:id" element={<WorkflowView />} />
        <Route path="/workflows" element={<WorkflowsView />} />
        {/* Approving the Word add-in's sign-in code: signed in only, so the
            auth overlay runs first and the path survives it. */}
        <Route path="/device" element={<DeviceApprovalView />} />
        <Route path="/" element={<Navigate to="/research" replace />} />
        <Route path="*" element={<NotFoundView />} />
      </Routes>
    </Suspense>
    </ErrorBoundary>
  );
}
