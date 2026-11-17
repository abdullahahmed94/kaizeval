import type { ReactNode } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { useSession } from "./auth";
import { Layout } from "./components/Layout";
import { Empty, Loading, PageHeader } from "./components/ui";
import { AgentDetailPage } from "./pages/AgentDetail";
import { AgentsPage } from "./pages/Agents";
import { AuditPage } from "./pages/Audit";
import { CandidatePage } from "./pages/Candidate";
import { DatasetDetailPage } from "./pages/DatasetDetail";
import { DatasetsPage } from "./pages/Datasets";
import { EvaluationDetailPage } from "./pages/EvaluationDetail";
import { EvaluationsPage } from "./pages/Evaluations";
import { DocumentPage, KnowledgePage } from "./pages/Knowledge";
import { LoginPage } from "./pages/Login";
import { ModelsPage } from "./pages/Models";
import { OptimizationDetailPage } from "./pages/OptimizationDetail";
import { OptimizationsPage } from "./pages/Optimizations";
import { OverviewPage } from "./pages/Overview";
import { RecommendationsPage } from "./pages/Recommendations";
import { SpecificationDetailPage } from "./pages/SpecificationDetail";
import { SpecificationsPage } from "./pages/Specifications";
import { WorkspacesPage } from "./pages/Workspaces";

/** Everything except the login page needs a session. */
function RequireSession({ children }: { children: ReactNode }) {
  const { loading, user, signedOut } = useSession();
  const location = useLocation();
  if (loading) return <Loading label="Checking your session…" />;
  if (!user) {
    return <Navigate to="/login" replace state={signedOut ? null : { from: location.pathname + location.search }} />;
  }
  return <>{children}</>;
}

/** Pages that work on workspace data need a workspace to exist first. */
function RequireWorkspace({ children }: { children: ReactNode }) {
  const { workspace } = useSession();
  if (!workspace) return <Navigate to="/workspaces" replace />;
  return <>{children}</>;
}

function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <Empty title="There is nothing at this address.">
        <Link to="/">Go to the overview</Link>
      </Empty>
    </>
  );
}

export function App() {
  const inWorkspace = (page: ReactNode) => <RequireWorkspace>{page}</RequireWorkspace>;
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireSession>
            <Layout />
          </RequireSession>
        }
      >
        <Route path="/workspaces" element={<WorkspacesPage />} />
        <Route path="/" element={inWorkspace(<OverviewPage />)} />
        <Route path="/models" element={inWorkspace(<ModelsPage />)} />
        <Route path="/agents" element={inWorkspace(<AgentsPage />)} />
        <Route path="/agents/:id" element={inWorkspace(<AgentDetailPage />)} />
        <Route path="/knowledge" element={inWorkspace(<KnowledgePage />)} />
        <Route path="/knowledge/:id" element={inWorkspace(<DocumentPage />)} />
        <Route path="/specifications" element={inWorkspace(<SpecificationsPage />)} />
        <Route path="/specifications/:id" element={inWorkspace(<SpecificationDetailPage />)} />
        <Route path="/datasets" element={inWorkspace(<DatasetsPage />)} />
        <Route path="/datasets/:id" element={inWorkspace(<DatasetDetailPage />)} />
        <Route path="/evaluations" element={inWorkspace(<EvaluationsPage />)} />
        <Route path="/evaluations/:id" element={inWorkspace(<EvaluationDetailPage />)} />
        <Route path="/recommendations" element={inWorkspace(<RecommendationsPage />)} />
        <Route path="/optimizations" element={inWorkspace(<OptimizationsPage />)} />
        <Route path="/optimizations/:id" element={inWorkspace(<OptimizationDetailPage />)} />
        <Route path="/candidates/:id" element={inWorkspace(<CandidatePage />)} />
        <Route path="/audit" element={inWorkspace(<AuditPage />)} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
