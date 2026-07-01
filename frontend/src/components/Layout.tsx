import type { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';

const navItems = [
  { path: '/config', label: 'Config', icon: '⚙️' },
  { path: '/telemetry', label: 'Telemetry', icon: '📊' },
  { path: '/diagnostics', label: 'Diagnostics', icon: '🔍' },
  { path: '/health', label: 'Health', icon: '❤️' },
];

export function Layout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-gray-50">
      <aside className="fixed left-0 top-0 bottom-0 w-56 bg-white border-r border-gray-200 p-4">
        <h1 className="text-lg font-bold text-gray-900 mb-6">AgentAlloy</h1>
        <nav className="space-y-1">
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                `flex items-center gap-2 px-3 py-2 rounded-md text-sm ${
                  isActive
                    ? 'bg-blue-50 text-blue-700 font-medium'
                    : 'text-gray-700 hover:bg-gray-100'
                }`
              }
            >
              <span>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="ml-56 p-6">{children}</main>
    </div>
  );
}
