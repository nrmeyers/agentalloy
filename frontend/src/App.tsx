import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import { Layout, ToastContainer } from './components';
import { ApprovalsPage } from './pages/ApprovalsPage';
import { ConfigPage } from './pages/ConfigPage';
import { DiagnosticsPage } from './pages/DiagnosticsPage';
import { HealthPage } from './pages/HealthPage';
import { OpsPage } from './pages/OpsPage';
import { PlaygroundPage } from './pages/PlaygroundPage';
import { ReposPage } from './pages/ReposPage';
import { SkillDetailPage } from './pages/SkillDetailPage';
import { SkillsPage } from './pages/SkillsPage';
import { TelemetryPage } from './pages/TelemetryPage';
import { WizardPage } from './pages/WizardPage';

export default function App() {
  return (
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Navigate to="/config" replace />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="/repos" element={<ReposPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/ops" element={<OpsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/skills/:skillId" element={<SkillDetailPage />} />
          <Route path="/wizard" element={<WizardPage />} />
          <Route path="/playground" element={<PlaygroundPage />} />
          <Route path="/telemetry" element={<TelemetryPage />} />
          <Route path="/diagnostics" element={<DiagnosticsPage />} />
          <Route path="/health" element={<HealthPage />} />
          <Route path="*" element={<Navigate to="/config" replace />} />
        </Routes>
      </Layout>
      <ToastContainer />
    </HashRouter>
  );
}
