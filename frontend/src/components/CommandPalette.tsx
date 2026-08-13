import { Command } from 'cmdk';
import { useEffect } from 'react';
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
  Play,
  RotateCw,
  ArrowRightLeft,
} from 'lucide-react';

interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNavigate: (path: string) => void;
}

interface PaletteItem {
  label: string;
  path: string;
  icon: typeof LayoutDashboard;
  keywords?: string[];
}

const pages: PaletteItem[] = [
  { label: 'Dashboard', path: '/', icon: LayoutDashboard, keywords: ['home', 'overview'] },
  { label: 'Code Index', path: '/code-index', icon: Database, keywords: ['search', 'symbols', 'graph'] },
  { label: 'Lifecycle', path: '/lifecycle', icon: GitBranch, keywords: ['phase', 'approve', 'workflow'] },
  { label: 'Contracts', path: '/contracts', icon: FileText, keywords: ['contract', 'scope', 'work item'] },
  { label: 'Skills', path: '/skills', icon: Puzzle, keywords: ['corpus', 'domain', 'system'] },
  { label: 'Playground', path: '/playground', icon: FlaskConical, keywords: ['test', 'retrieve', 'compose', 'signal'] },
  { label: 'Telemetry', path: '/telemetry', icon: BarChart3, keywords: ['traces', 'savings', 'coverage'] },
  { label: 'Config', path: '/config', icon: Settings, keywords: ['settings', 'llm', 'embedding'] },
  { label: 'System', path: '/system', icon: Server, keywords: ['health', 'diagnostics', 'doctor'] },
];

const actions = [
  { label: 'Trigger Reindex', icon: RotateCw, action: 'reindex' },
  { label: 'Advance Phase', icon: ArrowRightLeft, action: 'advance-phase' },
  { label: 'Open Playground', icon: Play, action: 'playground' },
];

export function CommandPalette({ open, onOpenChange, onNavigate }: CommandPaletteProps) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onOpenChange]);

  return (
    <Command.Dialog
      open={open}
      onOpenChange={onOpenChange}
      label="Command palette"
      className="fixed inset-0 z-50"
    >
      {open && (
        <div className="fixed inset-0 bg-black/50 dark:bg-black/70" onClick={() => onOpenChange(false)} />
      )}
      <div className="fixed left-1/2 top-[20%] -translate-x-1/2 w-full max-w-lg">
        <Command
          className="rounded-lg border border-[var(--border-primary)] bg-[var(--bg-elevated)] shadow-2xl overflow-hidden"
          onKeyDownCapture={(e) => {
            if (e.key === 'Escape') onOpenChange(false);
          }}
        >
          <div className="flex items-center border-b border-[var(--border-primary)] px-3">
            <Command.Input
              placeholder="Type a command or search…"
              className="h-11 w-full bg-transparent text-sm text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] outline-none"
              autoFocus
            />
          </div>
          <Command.List className="max-h-80 overflow-y-auto p-1.5">
            <Command.Empty className="py-6 text-center text-sm text-[var(--text-tertiary)]">
              No results found.
            </Command.Empty>

            <Command.Group heading="Pages" className="px-1.5 py-1">
              {pages.map((page) => (
                <Command.Item
                  key={page.path}
                  value={page.label}
                  keywords={page.keywords}
                  onSelect={() => onNavigate(page.path)}
                  className="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-[var(--text-primary)] cursor-pointer data-[selected=true]:bg-[var(--bg-tertiary)] transition-colors"
                >
                  <page.icon className="h-4 w-4 text-[var(--text-tertiary)]" />
                  {page.label}
                </Command.Item>
              ))}
            </Command.Group>

            <Command.Group heading="Actions" className="px-1.5 py-1 mt-1">
              {actions.map((a) => (
                <Command.Item
                  key={a.action}
                  value={a.label}
                  onSelect={() => {
                    if (a.action === 'playground') onNavigate('/playground');
                    if (a.action === 'advance-phase') onNavigate('/lifecycle');
                    if (a.action === 'reindex') onNavigate('/code-index');
                  }}
                  className="flex items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-[var(--text-primary)] cursor-pointer data-[selected=true]:bg-[var(--bg-tertiary)] transition-colors"
                >
                  <a.icon className="h-4 w-4 text-[var(--text-tertiary)]" />
                  {a.label}
                </Command.Item>
              ))}
            </Command.Group>
          </Command.List>
        </Command>
      </div>
    </Command.Dialog>
  );
}
