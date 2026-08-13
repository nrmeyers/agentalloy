import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { Layout, ToastContainer } from './components';
import { ThemeProvider } from './lib/theme';
import { CommandCenterPage } from './pages/CommandCenterPage';
import { ConfigPage } from './pages/ConfigPage';
import { ContractsPage } from './pages/ContractsPage';
import { CodeIndexPage } from './pages/CodeIndexPage';
import { LifecyclePage } from './pages/LifecyclePage';
import { SkillsPage } from './pages/SkillsPage';
import { SkillDetailPage } from './pages/SkillDetailPage';
import { WizardPage } from './pages/WizardPage';
import { PlaygroundPage } from './pages/PlaygroundPage';
import { TelemetryPage } from './pages/TelemetryPage';
import { SystemPage } from './pages/SystemPage';

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<CommandCenterPage />} />
            <Route path="/code-index" element={<CodeIndexPage />} />
            <Route path="/lifecycle" element={<LifecyclePage />} />
            <Route path="/contracts" element={<ContractsPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/skills/:skillId" element={<SkillDetailPage />} />
            <Route path="/wizard" element={<WizardPage />} />
            <Route path="/playground" element={<PlaygroundPage />} />
            <Route path="/telemetry" element={<TelemetryPage />} />
            <Route path="/config" element={<ConfigPage />} />
            <Route path="/system" element={<SystemPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
        <ToastContainer />
      </BrowserRouter>
    </ThemeProvider>
  );
}
