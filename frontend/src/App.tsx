import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import { Layout, ToastContainer } from './components';
import { ConfigPage } from './pages/ConfigPage';
import { DiagnosticsPage } from './pages/DiagnosticsPage';
import { HealthPage } from './pages/HealthPage';
import { PlaygroundPage } from './pages/PlaygroundPage';
import { SkillDetailPage } from './pages/SkillDetailPage';
import { SkillsPage } from './pages/SkillsPage';
import { TelemetryPage } from './pages/TelemetryPage';

export default function App() {
  return (
    <HashRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Navigate to="/config" replace />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/skills/:skillId" element={<SkillDetailPage />} />
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
