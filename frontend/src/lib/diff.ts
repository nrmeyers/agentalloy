// Tiny LCS-based line differ — no dependencies. Sized for skill prose
// (hundreds of lines), not for arbitrary large files.

export interface DiffLine {
  type: 'same' | 'add' | 'del';
  line: string;
}

const MAX_CELLS = 4_000_000; // ~2000×2000 lines — beyond that, bail to a trivial diff.

/**
 * Unified line diff of `before` → `after`. 'del' lines exist only in
 * `before`, 'add' lines only in `after`.
 */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split('\n');
  const b = after.split('\n');

  // Trim common prefix/suffix to keep the DP table small.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }

  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);
  const result: DiffLine[] = a.slice(0, start).map((line) => ({ type: 'same', line }));

  if ((midA.length + 1) * (midB.length + 1) > MAX_CELLS) {
    // Degenerate fallback: whole middle replaced.
    for (const line of midA) result.push({ type: 'del', line });
    for (const line of midB) result.push({ type: 'add', line });
  } else {
    result.push(...lcsDiff(midA, midB));
  }

  for (const line of a.slice(endA)) result.push({ type: 'same', line });
  return result;
}

function lcsDiff(a: string[], b: string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  // dp[i][j] = LCS length of a[i:], b[j:]
  const width = m + 1;
  const dp = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] =
        a[i] === b[j]
          ? dp[(i + 1) * width + j + 1] + 1
          : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', line: a[i] });
      i++;
      j++;
    } else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) {
      out.push({ type: 'del', line: a[i] });
      i++;
    } else {
      out.push({ type: 'add', line: b[j] });
      j++;
    }
  }
  while (i < n) out.push({ type: 'del', line: a[i++] });
  while (j < m) out.push({ type: 'add', line: b[j++] });
  return out;
}
