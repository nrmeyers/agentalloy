//! Lexical leg: tantivy BM25 full-text search.
//! Design §8.

use anyhow::Result;
use tantivy::{schema::*, collector::TopDocs, query::QueryParser, Index, IndexWriter};
use std::path::Path;

/// Build tantivy index from text documents.
#[allow(dead_code)]
pub fn build_bm25_index(index_dir: &Path, texts: &[&str]) -> Result<()> {
    let mut schema_builder = Schema::builder();
    let text_field = schema_builder.add_text_field("content", TEXT);
    let id_field = schema_builder.add_u64_field("id", INDEXED | STORED);
    let schema = schema_builder.build();

    let index = Index::create_in_dir(index_dir, schema)?;
    let mut index_writer: IndexWriter = index.writer(50_000_000)?;

    for (id, text) in texts.iter().enumerate() {
        let mut doc = TantivyDocument::new();
        doc.add_text(text_field, text);
        doc.add_u64(id_field, id as u64);
        index_writer.add_document(doc)?;
    }

    index_writer.commit()?;
    Ok(())
}

/// Search tantivy index with BM25 scoring.
#[allow(dead_code)]
pub fn search_bm25(index_dir: &Path, query: &str, k: usize) -> Result<Vec<usize>> {
    // TopDocs::with_limit panics on 0.
    let k = k.max(1);
    let index = Index::open_in_dir(index_dir)?;
    let schema = index.schema();
    let text_field = schema.get_field("content")?;
    let id_field = schema.get_field("id")?;

    let reader = index.reader()?;
    let searcher = reader.searcher();

    let query_parser = QueryParser::for_index(&index, vec![text_field]);
    let query = query_parser.parse_query(query)?;

    let top_docs = searcher.search(&query, &TopDocs::with_limit(k))?;
    let mut results = Vec::new();

    for (_score, doc_address) in top_docs {
        let doc: TantivyDocument = searcher.doc(doc_address)?;
        if let Some(id_value) = doc.get_first(id_field) {
            if let Some(id) = id_value.as_u64() {
                results.push(id as usize);
            }
        }
    }

    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_build_and_search_bm25() {
        let tmp = TempDir::new().unwrap();
        let texts = ["the quick brown fox", "jumps over the lazy dog", "rust is fast"];

        build_bm25_index(tmp.path(), &texts).unwrap();
        let results = search_bm25(tmp.path(), "fox", 10).unwrap();
        assert!(!results.is_empty());
        assert_eq!(results[0], 0); // "fox" is in doc 0
    }

    #[test]
    fn test_search_empty_index() {
        let tmp = TempDir::new().unwrap();
        build_bm25_index(tmp.path(), &[]).unwrap();
        let results = search_bm25(tmp.path(), "query", 10).unwrap();
        assert!(results.is_empty());
    }
}
