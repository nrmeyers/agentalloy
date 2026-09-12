//! Tree-sitter ingest: parse Python / Rust / TypeScript / TSX / JavaScript
//! files → Symbol nodes.
//! Design §8: Symbol nodes {fqn, file, line, kind} + Calls/Imports/Inherits/
//! Implements/Defines/HasMember edges.

use anyhow::Result;
use std::path::{Path, PathBuf};
use tree_sitter::{Language, Node, Parser};

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct Symbol {
    pub fqn: String,
    pub file: String,
    pub line: usize,
    pub kind: String, // "function", "class", "method", "interface", ...
}

/// Extensions the indexer understands.
pub const CODE_EXTENSIONS: [&str; 6] = ["py", "rs", "ts", "tsx", "js", "jsx"];

/// Map a file extension to its tree-sitter grammar (None → not indexed).
pub fn language_for_ext(ext: &str) -> Option<Language> {
    Some(match ext {
        "py" => tree_sitter_python::LANGUAGE.into(),
        "rs" => tree_sitter_rust::LANGUAGE.into(),
        "ts" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX.into(),
        // .jsx: parsed with the plain JS grammar — tree-sitter is
        // error-tolerant, JSX nodes become ERROR nodes and are skipped.
        "js" | "jsx" => tree_sitter_javascript::LANGUAGE.into(),
        _ => return None,
    })
}

/// Label a symbol node kind, or None if the node is not a symbol.
pub fn kind_label(node_kind: &str, in_type_body: bool) -> Option<&'static str> {
    Some(match node_kind {
        // Python methods are `function_definition` nodes; Rust methods are
        // `function_item` inside an impl — context decides the label.
        "function_definition" | "function_item" if in_type_body => "method",
        "function_definition" | "function_item" | "function_declaration" | "function" => {
            "function"
        }
        "class_definition" | "class_declaration" | "class" => "class",
        "method_definition" => "method",
        "struct_item" => "struct",
        // NOTE: impl_item is deliberately not a symbol — it has no `name`
        // field in tree-sitter-rust; it only provides method type context.
        "interface_declaration" => "interface",
        "type_alias_declaration" => "type",
        "enum_declaration" => "enum",
        _ => return None,
    })
}

/// Nodes whose bodies are not walked for nested symbols (nested functions
/// are intentionally not indexed at M1 granularity).
fn is_function_body(kind: &str) -> bool {
    matches!(
        kind,
        "function_definition" | "function_item" | "function_declaration" | "function"
    )
}

/// The name of the enclosing type a node introduces, if any: classes carry
/// a `name` field, Rust `impl` blocks a `type` field (`impl Foo`,
/// `impl Trait for Foo`).
fn enclosing_type_name<'a>(node: &Node, source: &'a str) -> Option<&'a str> {
    let field = match node.kind() {
        "class_definition" | "class_declaration" | "class" => "name",
        "impl_item" => "type",
        _ => return None,
    };
    node.child_by_field_name(field)
        .map(|n| &source[n.start_byte()..n.end_byte()])
}

/// Walk a syntax tree visiting every symbol node: top-level definitions,
/// class members (methods), interfaces, types, enums. Does not descend into
/// function bodies. The callback receives the node, a flag for whether it
/// sits inside a class/impl body (method labeling), and the enclosing type's
/// name (so method FQNs carry the class: `mod.Class.method`).
pub fn walk_symbols<'a>(
    node: &Node,
    source: &'a str,
    visit: &mut dyn FnMut(&Node, bool, Option<&'a str>),
) {
    walk_symbols_ctx(node, source, false, None, visit);
}

fn walk_symbols_ctx<'a>(
    node: &Node,
    source: &'a str,
    in_type_body: bool,
    type_name: Option<&'a str>,
    visit: &mut dyn FnMut(&Node, bool, Option<&'a str>),
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if kind_label(child.kind(), in_type_body).is_some() {
            visit(&child, in_type_body, type_name);
        }
        if is_function_body(child.kind()) {
            continue;
        }
        let (child_in_type, child_type_name) = if matches!(
            child.kind(),
            "class_definition" | "class_declaration" | "class" | "impl_item"
        ) {
            (true, enclosing_type_name(&child, source).or(type_name))
        } else {
            (in_type_body, type_name)
        };
        walk_symbols_ctx(&child, source, child_in_type, child_type_name, visit);
    }
}

/// Directories never indexed (build output, deps, caches).
fn is_ignored_component(component: &std::ffi::OsStr) -> bool {
    let s = component.to_string_lossy();
    s.starts_with('.')
        || s == "node_modules"
        || s == "__pycache__"
        || s == ".venv"
        || s == "target"
        || s == "dist"
        || s == ".next"
        || s == "out"
        || s == "coverage"
        || s == "vendor"
}

/// Walk repo_root yielding indexed code files (skips hidden/build/deps dirs).
pub fn iter_code_files(repo_root: &Path) -> Vec<PathBuf> {
    walkdir::WalkDir::new(repo_root)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .filter(|e| {
            let rel = e.path().strip_prefix(repo_root).unwrap_or(e.path());
            !rel.components().any(|c| is_ignored_component(c.as_os_str()))
        })
        .filter(|e| {
            e.path()
                .extension()
                .and_then(|ext| ext.to_str())
                .is_some_and(|ext| CODE_EXTENSIONS.contains(&ext))
        })
        .map(|e| e.path().to_path_buf())
        .collect()
}

/// Ingest a repository: walk files, parse with tree-sitter, extract symbols.
/// Unreadable or unparseable files are skipped, never fatal.
#[allow(dead_code)]
pub fn ingest_repo(repo_root: &Path) -> Result<Vec<Symbol>> {
    let mut symbols = Vec::new();

    for path in iter_code_files(repo_root) {
        let ext = match path.extension().and_then(|e| e.to_str()) {
            Some(e) => e.to_string(),
            None => continue,
        };
        let language = match language_for_ext(&ext) {
            Some(l) => l,
            None => continue,
        };

        let mut parser = Parser::new();
        if parser.set_language(&language).is_err() {
            continue;
        }

        let source = match std::fs::read_to_string(&path) {
            Ok(s) if !s.is_empty() => s,
            _ => continue, // binary, empty, or unreadable — skip
        };
        let tree = match parser.parse(&source, None) {
            Some(t) => t,
            None => continue, // parse failed — skip
        };

        let file_str = path.to_string_lossy().to_string();
        let module_fqn = path_to_fqn(&path, repo_root);
        let mut visit = |node: &Node, in_type_body: bool, type_name: Option<&str>| {
            let Some(kind) = kind_label(node.kind(), in_type_body) else {
                return;
            };
            let Some(name_node) = node.child_by_field_name("name") else {
                return;
            };
            let name = &source[name_node.start_byte()..name_node.end_byte()];
            // Methods carry the enclosing type: mod.Class.method — two
            // classes in one module with same-named methods must not collide.
            let fqn = match type_name {
                Some(t) => format!("{}.{}.{}", module_fqn, t, name),
                None => format!("{}.{}", module_fqn, name),
            };
            symbols.push(Symbol {
                fqn,
                file: file_str.clone(),
                line: node.start_position().row + 1,
                kind: kind.to_string(),
            });
        };
        walk_symbols(&tree.root_node(), &source, &mut visit);
    }

    Ok(symbols)
}

#[allow(dead_code)]
pub fn path_to_fqn(path: &Path, root: &Path) -> String {
    let relative = path.strip_prefix(root).unwrap_or(path);
    let stem = relative.with_extension("");
    stem.to_string_lossy().replace(std::path::MAIN_SEPARATOR, ".")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    #[test]
    fn test_ingest_empty_repo() {
        let tmp = TempDir::new().unwrap();
        let symbols = ingest_repo(tmp.path()).unwrap();
        assert!(symbols.is_empty());
    }

    #[test]
    fn test_ingest_python_file() {
        let tmp = TempDir::new().unwrap();
        let py_file = tmp.path().join("test.py");
        fs::write(
            &py_file,
            "def foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n",
        )
        .unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert_eq!(symbols.len(), 3);
        assert!(symbols.iter().any(|s| s.kind == "function" && s.fqn.contains("foo")));
        assert!(symbols.iter().any(|s| s.kind == "class" && s.fqn.contains("Bar")));
        assert!(symbols.iter().any(|s| s.kind == "method" && s.fqn.contains("baz")));
    }

    #[test]
    fn test_ingest_rust_file() {
        let tmp = TempDir::new().unwrap();
        let rs_file = tmp.path().join("test.rs");
        fs::write(&rs_file, "fn main() {}\nstruct Foo {}\nimpl Foo {\n    fn bar(&self) {}\n}\n")
            .unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert_eq!(symbols.len(), 3);
        assert!(symbols.iter().any(|s| s.kind == "function" && s.fqn.contains("main")));
        assert!(symbols.iter().any(|s| s.kind == "struct" && s.fqn.contains("Foo")));
        assert!(symbols.iter().any(|s| s.kind == "method" && s.fqn.contains("bar")));
    }

    #[test]
    fn test_ingest_typescript_file() {
        let tmp = TempDir::new().unwrap();
        let ts_file = tmp.path().join("src").join("service.ts");
        fs::create_dir_all(tmp.path().join("src")).unwrap();
        fs::write(
            &ts_file,
            r#"
export interface User {
  id: string;
}

export type UserId = string;

export enum Role {
  Admin = "admin",
}

export class UserService {
  constructor(private repo: any) {}
  async find(id: string): Promise<User | null> {
    return this.repo.get(id);
  }
}

export function createUser(name: string): User {
  return { id: name };
}
"#,
        )
        .unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert!(symbols.iter().any(|s| s.kind == "interface" && s.fqn.contains("User")));
        assert!(symbols.iter().any(|s| s.kind == "type" && s.fqn.contains("UserId")));
        assert!(symbols.iter().any(|s| s.kind == "enum" && s.fqn.contains("Role")));
        assert!(symbols.iter().any(|s| s.kind == "class" && s.fqn.contains("UserService")));
        assert!(symbols.iter().any(|s| s.kind == "method" && s.fqn.contains("find")));
        assert!(symbols.iter().any(|s| s.kind == "function" && s.fqn.contains("createUser")));
        // FQNs are module-qualified, dots for path separators
        let find = symbols
            .iter()
            .find(|s| s.kind == "method" && s.fqn.contains("find"))
            .unwrap();
        assert!(find.fqn.starts_with("src.service"));
    }

    #[test]
    fn test_ingest_tsx_file() {
        let tmp = TempDir::new().unwrap();
        let tsx_file = tmp.path().join("App.tsx");
        fs::write(
            &tsx_file,
            "export function App() {\n  return <div>hi</div>;\n}\n",
        )
        .unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert!(symbols
            .iter()
            .any(|s| s.kind == "function" && s.fqn.contains("App")));
    }

    #[test]
    fn test_ingest_javascript_file() {
        let tmp = TempDir::new().unwrap();
        let js_file = tmp.path().join("util.js");
        fs::write(&js_file, "function clamp(x) { return x; }\nclass Store {}\n").unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert_eq!(symbols.len(), 2);
        assert!(symbols.iter().any(|s| s.kind == "function" && s.fqn.contains("clamp")));
        assert!(symbols.iter().any(|s| s.kind == "class" && s.fqn.contains("Store")));
    }

    #[test]
    fn test_ingest_skips_binary_and_unparseable_files() {
        let tmp = TempDir::new().unwrap();
        fs::write(tmp.path().join("ok.ts"), "export function a() {}\n").unwrap();
        // Invalid UTF-8 — read_to_string fails, must be skipped not fatal
        fs::write(tmp.path().join("binary.ts"), [0xFF, 0xFE, 0x00, 0x01]).unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert_eq!(symbols.len(), 1);
    }

    #[test]
    fn test_ingest_skips_node_modules_and_hidden() {
        let tmp = TempDir::new().unwrap();
        let nm = tmp.path().join("node_modules").join("dep");
        fs::create_dir_all(&nm).unwrap();
        fs::write(nm.join("index.ts"), "export function hidden() {}\n").unwrap();
        let dot = tmp.path().join(".git");
        fs::create_dir_all(&dot).unwrap();
        fs::write(dot.join("hook.py"), "def nope():\n    pass\n").unwrap();
        fs::write(tmp.path().join("real.ts"), "export function real() {}\n").unwrap();

        let symbols = ingest_repo(tmp.path()).unwrap();
        assert_eq!(symbols.len(), 1);
        assert!(symbols[0].fqn.contains("real"));
    }

    #[test]
    fn test_language_for_ext() {
        assert!(language_for_ext("ts").is_some());
        assert!(language_for_ext("tsx").is_some());
        assert!(language_for_ext("js").is_some());
        assert!(language_for_ext("jsx").is_some());
        assert!(language_for_ext("py").is_some());
        assert!(language_for_ext("rs").is_some());
        assert!(language_for_ext("md").is_none());
        assert!(language_for_ext("yaml").is_none());
    }
}
