import { useState, useCallback, useEffect, type ReactNode } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Database,
  GitBranch,
  FileText,
  Puzzle,
  FlaskConical,
  BarChart3,
  Settings,
  Server,
  Moon,
  Sun,
  Command,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import { useApprovals } from '../hooks/useRepos';
import { useTheme } from '../lib/theme';
import { CommandPalette } from './CommandPalette';

interface NavItem {
  path: string;
  label: string;
  icon: typeof LayoutDashboard;
  section: string;
}

const navItems: NavItem[] = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard, section: 'Overview' },
  { path: '/code-index', label: 'Code Index', icon: Database, section: 'Intelligence' },
  { path: '/lifecycle', label: 'Lifecycle', icon: GitBranch, section: 'Lifecycle' },
  { path: '/contracts', label: 'Contracts', icon: FileText, section: 'Lifecycle' },
  { path: '/skills', label: 'Skills', icon: Puzzle, section: 'System' },
  { path: '/playground', label: 'Playground', icon: FlaskConical, section: 'System' },
  { path: '/telemetry', label: 'Telemetry', icon: BarChart3, section: 'System' },
  { path: '/config', label: 'Config', icon: Settings, section: 'System' },
  { path: '/system', label: 'System', icon: Server, section: 'System' },
];

const sections = ['Overview', 'Intelligence', 'Lifecycle', 'System'];

function ApprovalsBadge() {
  const { data } = useApprovals();
  const count = data?.total ?? 0;
  if (count === 0) return null;
  return (
    <span className="ml-auto inline-flex items-center justify-center min-w-[1.125rem] h-[1.125rem] px-1 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 text-[10px] font-semibold tabular-nums">
      {count}
    </span>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setPaletteOpen((prev) => !prev);
      }
    },
    [],
  );

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  return (
    <div className="min-h-screen bg-[var(--bg-primary)]">
      {/* Sidebar */}
      <aside
        className={`fixed left-0 top-0 bottom-0 flex flex-col border-r border-[var(--sidebar-border)] bg-[var(--sidebar-bg)] z-30
          transition-all duration-200 ease-in-out
          ${collapsed ? 'w-14' : 'w-56'}`}
      >
        {/* Header */}
        <div className="flex items-center h-12 px-3 border-b border-[var(--border-primary)] shrink-0">
          {!collapsed && (
            <span className="text-sm font-semibold text-[var(--text-primary)] tracking-tight truncate">
              AgentAlloy
            </span>
          )}
          <button
            onClick={() => setCollapsed((c) => !c)}
            className={`p-1 rounded text-[var(--text-tertiary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors
              ${collapsed ? 'mx-auto' : 'ml-auto'}`}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? (
              <ChevronRight className="h-3.5 w-3.5" />
            ) : (
              <ChevronLeft className="h-3.5 w-3.5" />
            )}
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto py-2 px-2">
          {sections.map((section) => {
            const items = navItems.filter((n) => n.section === section);
            if (items.length === 0) return null;
            return (
              <div key={section} className="mb-2">
                {!collapsed && (
                  <p className="px-2 py-1 text-[10px] font-medium uppercase tracking-widest text-[var(--text-tertiary)]">
                    {section}
                  </p>
                )}
                {items.map((item) => (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.path === '/'}
                    className={({ isActive }) =>
                      `flex items-center gap-2.5 rounded-md text-sm transition-colors
                      ${collapsed ? 'px-2 py-2 justify-center' : 'px-2 py-1.5'}
                      ${
                        isActive
                          ? 'bg-brand-500/10 text-brand-600 dark:text-brand-400 font-medium'
                          : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
                      }`
                    }
                  >
                    <item.icon className="h-4 w-4 shrink-0" />
                    {!collapsed && (
                      <>
                        <span className="truncate">{item.label}</span>
                        {item.path === '/lifecycle' && <ApprovalsBadge />}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="border-t border-[var(--border-primary)] p-2 flex items-center gap-1 shrink-0">
          <button
            onClick={() => setPaletteOpen(true)}
            className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors
              ${collapsed ? 'justify-center w-full' : 'flex-1'}`}
            title="Command palette (⌘K)"
          >
            <Command className="h-3.5 w-3.5" />
            {!collapsed && <span className="truncate">Command…</span>}
          </button>
          <button
            onClick={toggleTheme}
            className="p-1.5 rounded-md text-[var(--text-tertiary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
          >
            {theme === 'dark' ? (
              <Sun className="h-3.5 w-3.5" />
            ) : (
              <Moon className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main
        className={`transition-all duration-200 ease-in-out ${collapsed ? 'ml-14' : 'ml-56'}`}
      >
        <div className="p-6 max-w-[1400px] mx-auto">{children}</div>
      </main>

      {/* Command Palette */}
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        onNavigate={(path) => {
          setPaletteOpen(false);
          navigate(path);
        }}
      />
    </div>
  );
}
