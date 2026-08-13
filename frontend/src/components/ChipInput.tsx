import { useState } from 'react';
import { X } from 'lucide-react';

export function ChipInput({
  values,
  onChange,
  placeholder = 'add tag…',
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState('');

  const add = () => {
    const value = draft.trim();
    setDraft('');
    if (value === '' || values.includes(value)) return;
    onChange([...values, value]);
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5 border border-[var(--border-primary)] rounded-md bg-[var(--bg-primary)]">
      {values.map((value) => (
        <span
          key={value}
          className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-[var(--bg-tertiary)] text-[var(--text-primary)]"
        >
          {value}
          <button
            type="button"
            aria-label={`Remove ${value}`}
            onClick={() => onChange(values.filter((v) => v !== value))}
            className="text-[var(--text-tertiary)] hover:text-[var(--text-primary)] transition-colors"
          >
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            add();
          } else if (e.key === 'Backspace' && draft === '' && values.length > 0) {
            onChange(values.slice(0, -1));
          }
        }}
        onBlur={add}
        className="flex-1 min-w-[8rem] text-sm bg-transparent text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] focus:outline-none"
      />
    </div>
  );
}
