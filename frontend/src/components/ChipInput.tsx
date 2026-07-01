import { useState } from 'react';

/**
 * Editable chip list — type a value and press Enter (or comma) to add,
 * click × to remove. Used for domain_tags editing.
 */
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
    <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5 border border-gray-300 rounded-md bg-white">
      {values.map((value) => (
        <span
          key={value}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-700"
        >
          {value}
          <button
            type="button"
            aria-label={`Remove ${value}`}
            onClick={() => onChange(values.filter((v) => v !== value))}
            className="text-gray-400 hover:text-gray-700"
          >
            ×
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
        className="flex-1 min-w-[8rem] text-sm focus:outline-none"
      />
    </div>
  );
}
