use pyo3::prelude::*;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

/// Unique suffix for tantivy build dirs: two ingests in one process (even
/// serialized) must never share a build dir with a stale one.
static BUILD_SEQ: AtomicU64 = AtomicU64::new(0);

mod dense;
mod fusion;
mod ingest;
mod lexical;
mod validate;

use ingest::{ingest_repo, path_to_fqn, Symbol, walk_symbols, kind_label, iter_code_files, language_for_ext};

/// A chunk of source code that can be searched.
#[derive(Clone, Debug)]
struct CodeChunk {
    file: String,
    line: usize,
    content: String,
    symbol_fqn: Option<String>,
}

/// Files larger than this are skipped (minified/generated output is noise).
const MAX_CHUNK_FILE_BYTES: usize = 256_000;
/// Embedding input truncation (fits the embed model's token limit).
const EMBED_INPUT_CHARS: usize = 1000;

/// Inner mutable state for the DataLayer (behind Mutex for PyO3 thread safety).
/// INVARIANT: `vectors[i]` belongs to `chunks[i]`; an empty vector means the
/// chunk was indexed without the embedding server (lexical-only leg).
struct IndexState {
    symbols: Vec<Symbol>,
    chunks: Vec<CodeChunk>,
    vectors: Vec<Vec<f32>>,
    tantivy_dir: Option<PathBuf>,
    embed_url: String,
}

impl IndexState {
    fn new() -> Self {
        Self {
            symbols: Vec::new(),
            chunks: Vec::new(),
            vectors: Vec::new(),
            tantivy_dir: None,
            embed_url: String::new(),
        }
    }
}

/// Core data layer: code index (tree-sitter + tantivy + dense vectors) + DuckDB RO.
/// Python owns the single RW handle for DuckDB; Rust opens RO only (AC-7 boundary).
///
/// The index is multi-repo: `build_index` resets and indexes one repo,
/// `ingest` adds (or refreshes) one repo into the existing index.
#[pyclass]
pub struct DataLayer {
    index_dir: PathBuf,
    state: Mutex<IndexState>,
    /// Serializes whole ingests: concurrent ingests would interleave their
    /// merge/build/swap phases and corrupt the chunk↔tantivy id alignment.
    ingest_lock: Mutex<()>,
}

#[pymethods]
impl DataLayer {
    #[new]
    fn new(index_dir: String) -> PyResult<Self> {
        Ok(Self {
            index_dir: PathBuf::from(index_dir),
            state: Mutex::new(IndexState::new()),
            ingest_lock: Mutex::new(()),
        })
    }

    /// Build the code index from a repository root (resets any previous index).
    /// Ingests Python / Rust / TypeScript / TSX / JavaScript files via
    /// tree-sitter, builds dense + lexical legs.
    /// embed_url: embedding server URL (e.g., http://localhost:48951).
    /// If embed_url is empty or unreachable, builds a lexical-only index.
    fn build_index(&self, repo_root: String, embed_url: String) -> String {
        let mut guard = match self.state.lock() {
            Ok(g) => g,
            Err(e) => return error_json(&format!("lock failed: {e}")),
        };
        *guard = IndexState::new();
        drop(guard);
        self.ingest(repo_root, embed_url)
    }

    /// Add (or refresh) one repository into the existing multi-repo index.
    /// Re-ingesting a repo already present replaces its entries — idempotent.
    /// Rebuilds the BM25 leg over all chunks; embeds only the new repo's
    /// chunks. Returns the totals after the merge.
    fn ingest(&self, repo_root: String, embed_url: String) -> String {
        let root = match std::path::Path::new(&repo_root).canonicalize() {
            Ok(p) => p,
            Err(e) => return error_json(&format!("repo not found: {repo_root}: {e}")),
        };
        if !root.is_dir() {
            return error_json(&format!("repo not a directory: {repo_root}"));
        }

        // 1+2: symbols and chunks (heavy I/O, no lock held)
        let new_symbols = match ingest_repo(&root) {
            Ok(s) => s,
            Err(e) => return error_json(&format!("ingest failed: {e}")),
        };
        let new_chunks = build_chunks(&root);

        // 3: dense vectors for the new chunks only (sentinel = empty vec)
        let new_vectors = if embed_url.is_empty() || new_chunks.is_empty() {
            vec![Vec::new(); new_chunks.len()]
        } else {
            let embed_texts: Vec<String> = new_chunks
                .iter()
                .map(|c| truncate_chars(&c.content, EMBED_INPUT_CHARS))
                .collect();
            match dense::embed_texts(&embed_texts, &embed_url) {
                Ok(raw) => raw
                    .iter()
                    .map(|v| dense::truncate_and_renorm(v, 768))
                    .collect(),
                Err(_) => vec![Vec::new(); new_chunks.len()], // lexical-only for this repo
            }
        };

        // 4: serialize whole ingests, then STAGE the merge without touching
        // live state — chunks and the tantivy index must swap together (the
        // tantivy doc ids are positions in `chunks`, so a mutated chunk list
        // with the old index maps ids onto the wrong rows).
        let _ingest_guard = match self.ingest_lock.lock() {
            Ok(g) => g,
            Err(e) => return error_json(&format!("ingest lock failed: {e}")),
        };
        let prefix = format!("{}/", root.display());
        let (mut staged_symbols, mut staged_chunks, mut staged_vectors, prior_embed_url) = {
            let state = match self.state.lock() {
                Ok(s) => s,
                Err(e) => return error_json(&format!("lock failed: {e}")),
            };
            let mut kept_chunks = Vec::new();
            let mut kept_vectors = Vec::new();
            for (c, v) in state.chunks.iter().zip(state.vectors.iter()) {
                if !c.file.starts_with(&prefix) {
                    kept_chunks.push(c.clone());
                    kept_vectors.push(v.clone());
                }
            }
            let kept_symbols: Vec<Symbol> = state
                .symbols
                .iter()
                .filter(|s| !s.file.starts_with(&prefix))
                .cloned()
                .collect();
            (kept_symbols, kept_chunks, kept_vectors, state.embed_url.clone())
        };
        staged_symbols.extend(new_symbols);
        staged_chunks.extend(new_chunks);
        staged_vectors.extend(new_vectors);

        let symbol_count = staged_symbols.len();
        let chunk_count = staged_chunks.len();
        let hybrid = staged_vectors.iter().any(|v| !v.is_empty());

        // 5: build the BM25 leg over ALL staged chunks into a fresh dir.
        // Failure leaves live state untouched.
        let tantivy_dir = self.index_dir.join("tantivy");
        let build_dir = self.index_dir.join(format!(
            "tantivy-build-{}-{}",
            std::process::id(),
            BUILD_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&build_dir);
        if let Err(e) = std::fs::create_dir_all(&build_dir) {
            return error_json(&format!("mkdir failed: {e}"));
        }
        let all_texts: Vec<&str> = staged_chunks.iter().map(|c| c.content.as_str()).collect();
        if let Err(e) = lexical::build_bm25_index(&build_dir, &all_texts) {
            let _ = std::fs::remove_dir_all(&build_dir);
            return error_json(&format!("bm25 build failed: {e}"));
        }
        drop(all_texts);

        // 6: publish chunks + tantivy dir under ONE state lock — a search
        // never sees new chunks with the old index or vice versa.
        let mut state = match self.state.lock() {
            Ok(s) => s,
            Err(e) => return error_json(&format!("lock failed: {e}")),
        };
        let _ = std::fs::remove_dir_all(&tantivy_dir);
        if let Err(e) = std::fs::rename(&build_dir, &tantivy_dir) {
            let _ = std::fs::remove_dir_all(&build_dir);
            return error_json(&format!("tantivy swap failed: {e}"));
        }
        state.symbols = staged_symbols;
        state.chunks = staged_chunks;
        state.vectors = staged_vectors;
        state.embed_url = if embed_url.is_empty() { prior_embed_url } else { embed_url };
        state.tantivy_dir = Some(tantivy_dir);
        drop(state);

        let mode = if hybrid { "hybrid" } else { "lexical-only" };
        format!(
            r#"{{"status":"ok","symbols":{symbol_count},"chunks":{chunk_count},"mode":"{mode}"}}"#
        )
    }

    /// Semantic code search: hybrid (dense + lexical) with RRF fusion.
    /// Returns JSON array of {file, line, content, symbol} objects
    /// (symbol is the chunk's FQN, or null for file-level chunks).
    fn code_search(&self, py: Python<'_>, query: String, k: usize) -> String {
        let k = k.max(1);

        // Embed the query WITHOUT holding the state lock: the HTTP call can
        // take up to 120s and would block every other DataLayer call.
        let (embed_url, has_vectors) = {
            let state = match self.state.lock() {
                Ok(s) => s,
                Err(e) => return error_json(&format!("lock failed: {e}")),
            };
            (
                state.embed_url.clone(),
                state.vectors.iter().any(|v| !v.is_empty()),
            )
        };
        let query_vec = if has_vectors {
            let url = if embed_url.is_empty() {
                "http://localhost:48951".to_string()
            } else {
                embed_url
            };
            py.allow_threads(|| {
                dense::embed_texts(std::slice::from_ref(&query), &url)
                    .ok()
                    .and_then(|v| v.into_iter().next())
                    .map(|v| dense::truncate_and_renorm(&v, 768))
                    // Short-vector sentinel from truncate_and_renorm.
                    .filter(|v| !v.is_empty())
            })
        } else {
            None
        };

        let state = match self.state.lock() {
            Ok(s) => s,
            Err(e) => return error_json(&format!("lock failed: {e}")),
        };

        // Dense leg: only over chunks that have vectors (zero-copy borrows).
        let pool: Vec<&[f32]> = state
            .vectors
            .iter()
            .filter(|v| !v.is_empty())
            .map(|v| v.as_slice())
            .collect();
        let pool_to_chunk: Vec<usize> = state
            .vectors
            .iter()
            .enumerate()
            .filter(|(_, v)| !v.is_empty())
            .map(|(i, _)| i)
            .collect();
        let mut dense_results: Vec<usize> = vec![];
        if !pool.is_empty() {
            if let Some(qv) = query_vec {
                let local = dense::search_vectors(&qv, &pool, k * 2);
                dense_results = local.into_iter().map(|i| pool_to_chunk[i]).collect();
            }
        }

        // Lexical leg
        let mut lexical_results: Vec<usize> = vec![];
        if let Some(ref tantivy_dir) = state.tantivy_dir {
            if let Ok(results) = lexical::search_bm25(tantivy_dir, &query, k * 2) {
                lexical_results = results;
            }
        }

        // RRF fusion
        let fused = if !dense_results.is_empty() && !lexical_results.is_empty() {
            fusion::rrf_fusion(&dense_results, &lexical_results, k)
        } else if !dense_results.is_empty() {
            dense_results.into_iter().take(k).collect()
        } else {
            lexical_results.into_iter().take(k).collect()
        };

        // Build JSON results using serde_json for proper escaping
        let results: Vec<serde_json::Value> = fused
            .iter()
            .filter_map(|&idx| state.chunks.get(idx))
            .map(|chunk| {
                let mut obj = serde_json::Map::new();
                obj.insert(
                    "file".to_string(),
                    serde_json::Value::String(chunk.file.clone()),
                );
                obj.insert(
                    "line".to_string(),
                    serde_json::Value::Number(chunk.line.into()),
                );
                // Truncate content to 200 chars for search results
                let content = if chunk.content.len() > 200 {
                    let end = truncate_chars(&chunk.content, 200).len();
                    format!("{}...", &chunk.content[..end])
                } else {
                    chunk.content.clone()
                };
                obj.insert(
                    "content".to_string(),
                    serde_json::Value::String(content),
                );
                match &chunk.symbol_fqn {
                    Some(fqn) => {
                        obj.insert("symbol".to_string(), serde_json::Value::String(fqn.clone()));
                    }
                    None => {
                        obj.insert("symbol".to_string(), serde_json::Value::Null);
                    }
                }
                serde_json::Value::Object(obj)
            })
            .collect();

        serde_json::to_string(&results).unwrap_or_else(|_| "[]".to_string())
    }

    /// Symbol lookup by FQN (substring match).
    /// Returns JSON array of {fqn, file, line, kind} objects.
    fn symbols(&self, fqn: String) -> String {
        let state = match self.state.lock() {
            Ok(s) => s,
            Err(_) => return "[]".to_string(),
        };

        let matches: Vec<&Symbol> = if fqn.is_empty() {
            state.symbols.iter().collect()
        } else {
            state
                .symbols
                .iter()
                .filter(|s| s.fqn.contains(&fqn))
                .collect()
        };

        let results: Vec<serde_json::Value> = matches
            .iter()
            .map(|s| {
                let mut obj = serde_json::Map::new();
                obj.insert("fqn".to_string(), serde_json::Value::String(s.fqn.clone()));
                obj.insert("file".to_string(), serde_json::Value::String(s.file.clone()));
                obj.insert(
                    "line".to_string(),
                    serde_json::Value::Number(s.line.into()),
                );
                obj.insert(
                    "kind".to_string(),
                    serde_json::Value::String(s.kind.clone()),
                );
                serde_json::Value::Object(obj)
            })
            .collect();

        serde_json::to_string(&results).unwrap_or_else(|_| "[]".to_string())
    }

    /// Skill candidates retrieval (stub — needs skill corpus in M2).
    fn skill_candidates(&self, _task: String, _phase: String, _k: usize) -> Vec<String> {
        vec![]
    }

    /// Get contract by slug (DuckDB RO read — stub, Python owns DuckDB).
    fn get_contract(&self, _slug: String, _duck_path: String) -> Option<String> {
        None
    }

    /// List artifacts (DuckDB RO read — stub).
    fn list_artifacts(&self, _phase: String, _slug: String, _duck_path: String) -> Vec<String> {
        vec![]
    }

    /// Get telemetry traces (DuckDB RO read — stub).
    #[pyo3(signature = (_duck_path, _k, _phase=None))]
    fn traces(
        &self,
        _duck_path: String,
        _k: usize,
        _phase: Option<String>,
    ) -> Vec<String> {
        vec![]
    }

    /// Index-grounded validation: check if symbol exists, slug known, etc.
    /// Fail-open on store error (AC-13).
    fn validate(&self, check_type: String, value: String) -> bool {
        match check_type.as_str() {
            "symbol_exists" => {
                let state = self.state.lock().ok();
                match state {
                    Some(s) => s.symbols.iter().any(|sym| sym.fqn.contains(&value)),
                    None => true, // fail-open
                }
            }
            "slug_known" => true, // fail-open (DuckDB is Python's domain)
            _ => true, // fail-open for unknown check types
        }
    }

    /// Return the number of indexed symbols.
    fn symbol_count(&self) -> usize {
        self.state.lock().map(|s| s.symbols.len()).unwrap_or(0)
    }

    /// Return the number of indexed chunks.
    fn chunk_count(&self) -> usize {
        self.state.lock().map(|s| s.chunks.len()).unwrap_or(0)
    }
}

fn error_json(message: &str) -> String {
    // serde_json escapes quotes/backslashes in OS error strings and paths —
    // hand-rolled interpolation produced invalid JSON for those.
    serde_json::json!({"status": "error", "message": message}).to_string()
}

/// Truncate to n chars on a char boundary.
fn truncate_chars(s: &str, n: usize) -> String {
    if s.len() <= n {
        return s.to_string();
    }
    let end = s
        .char_indices()
        .take_while(|&(i, _)| i < n)
        .last()
        .map(|(i, ch)| i + ch.len_utf8())
        .unwrap_or(0);
    s[..end].to_string()
}

/// Build code chunks from source files in the repo: one file-level chunk per
/// file plus one chunk per symbol (function / class / method / interface).
fn build_chunks(repo_root: &std::path::Path) -> Vec<CodeChunk> {
    let mut chunks = Vec::new();

    for path in iter_code_files(repo_root) {
        let source = match std::fs::read_to_string(&path) {
            Ok(s) if !s.trim().is_empty() && s.len() <= MAX_CHUNK_FILE_BYTES => s,
            _ => continue,
        };
        let file = path.to_string_lossy().to_string();

        // File-level chunk
        chunks.push(CodeChunk {
            file: file.clone(),
            line: 1,
            content: source.clone(),
            symbol_fqn: None,
        });

        // Per-symbol chunks
        let ext = match path.extension().and_then(|e| e.to_str()) {
            Some(e) => e.to_string(),
            None => continue,
        };
        let language = match language_for_ext(&ext) {
            Some(l) => l,
            None => continue,
        };
        let mut parser = tree_sitter::Parser::new();
        if parser.set_language(&language).is_err() {
            continue;
        }
        let tree = match parser.parse(&source, None) {
            Some(t) => t,
            None => continue,
        };
        let module_fqn = path_to_fqn(&path, repo_root);
        let mut visit = |node: &tree_sitter::Node, in_type_body: bool, type_name: Option<&str>| {
            if kind_label(node.kind(), in_type_body).is_none() {
                return;
            }
            let Some(name_node) = node.child_by_field_name("name") else {
                return;
            };
            let name = &source[name_node.start_byte()..name_node.end_byte()];
            let fqn = match type_name {
                Some(t) => format!("{module_fqn}.{t}.{name}"),
                None => format!("{module_fqn}.{name}"),
            };
            let content = &source[node.start_byte()..node.end_byte()];
            chunks.push(CodeChunk {
                file: file.clone(),
                line: node.start_position().row + 1,
                content: content.to_string(),
                symbol_fqn: Some(fqn),
            });
        };
        walk_symbols(&tree.root_node(), &source, &mut visit);
    }

    chunks
}

/// Python module exposed as agentalloy._core
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<DataLayer>()?;
    Ok(())
}
