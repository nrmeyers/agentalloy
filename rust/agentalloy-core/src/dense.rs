//! Dense leg: embedding via :48951, MRL 1024→768 truncation + L2-renorm,
//! brute-force vector search (pre-normalized inner product).
//! Design §8, §9.
//!
//! The brute-force search is O(n) per query but correct and dependency-free.
//! For M1 codebases (<10k files), this is fast enough. M2 can swap in HNSW.

use anyhow::Result;
use serde::{Deserialize, Serialize};

#[derive(Serialize)]
struct EmbedRequest {
    input: Vec<String>,
    model: String,
}

#[derive(Deserialize)]
struct EmbedResponse {
    data: Vec<EmbedData>,
}

#[derive(Deserialize)]
struct EmbedData {
    embedding: Vec<f32>,
}

/// Call the embedding server (:48951) to get vectors for input texts.
/// Returns 1024-dim vectors; caller truncates to 768 + L2-renorms.
/// Batches into groups of 32 to avoid payload size limits.
#[allow(dead_code)]
pub fn embed_texts(texts: &[String], embed_url: &str) -> Result<Vec<Vec<f32>>> {
    if texts.is_empty() {
        return Ok(vec![]);
    }

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .build()?;

    let mut all_embeddings: Vec<Vec<f32>> = Vec::with_capacity(texts.len());
    let batch_size = 16;  // Conservative: embedding server has 512-token batch limit

    for chunk in texts.chunks(batch_size) {
        let request = EmbedRequest {
            input: chunk.to_vec(),
            model: "LFM2.5-Embedding-350M".to_string(),
        };

        let response: EmbedResponse = client
            .post(format!("{}/v1/embeddings", embed_url))
            .json(&request)
            .send()?
            .json()?;

        all_embeddings.extend(response.data.into_iter().map(|d| d.embedding));
    }

    Ok(all_embeddings)
}

/// Truncate 1024-dim vector to 768 (MRL) and L2-renormalize.
/// A vector shorter than `target_dim` (misconfigured embed server, error
/// payload) returns the empty-vec sentinel — "no vector for this chunk" —
/// instead of panicking across the PyO3 boundary.
#[allow(dead_code)]
pub fn truncate_and_renorm(vec: &[f32], target_dim: usize) -> Vec<f32> {
    if vec.len() < target_dim {
        return Vec::new();
    }
    let truncated = &vec[..target_dim];
    let norm: f32 = truncated.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm == 0.0 {
        return truncated.to_vec();
    }
    truncated.iter().map(|x| x / norm).collect()
}

/// Brute-force nearest-neighbor search over pre-normalized vectors.
/// Returns indices of top-k most similar vectors (by inner product).
#[allow(dead_code)]
pub fn search_vectors(query: &[f32], vectors: &[&[f32]], k: usize) -> Vec<usize> {
    if vectors.is_empty() || k == 0 {
        return vec![];
    }

    let mut scored: Vec<(usize, f32)> = vectors
        .iter()
        .enumerate()
        .map(|(i, v)| {
            let score: f32 = query
                .iter()
                .zip(v.iter())
                .map(|(a, b)| a * b)
                .sum();
            (i, score)
        })
        .collect();

    // total_cmp: NaN scores (NaN embeddings from the server) must not panic.
    scored.sort_by(|a, b| b.1.total_cmp(&a.1));
    scored.into_iter().take(k).map(|(i, _)| i).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_truncate_and_renorm() {
        let vec = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let result = truncate_and_renorm(&vec, 3);
        assert_eq!(result.len(), 3);
        let norm: f32 = result.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 1e-5);
    }

    #[test]
    fn test_truncate_short_vector_is_sentinel() {
        // Shorter than target: empty sentinel, no panic.
        assert!(truncate_and_renorm(&[1.0, 2.0], 768).is_empty());
    }

    #[test]
    fn test_truncate_zero_vector() {
        let vec = vec![0.0, 0.0, 0.0, 0.0];
        let result = truncate_and_renorm(&vec, 2);
        assert_eq!(result.len(), 2);
        assert_eq!(result, vec![0.0, 0.0]);
    }

    #[test]
    fn test_search_vectors_basic() {
        let vectors = vec![
            vec![1.0, 0.0, 0.0],
            vec![0.0, 1.0, 0.0],
            vec![0.707, 0.707, 0.0],
        ];
        let pool: Vec<&[f32]> = vectors.iter().map(|v| v.as_slice()).collect();
        let query = vec![0.9, 0.1, 0.0];
        let results = search_vectors(&query, &pool, 2);
        assert_eq!(results[0], 0); // closest to [1,0,0]
        assert_eq!(results[1], 2); // next closest to [0.707,0.707,0]
    }

    #[test]
    fn test_search_vectors_empty() {
        let results = search_vectors(&[1.0, 0.0], &[], 5);
        assert!(results.is_empty());
    }

    #[test]
    fn test_search_vectors_k_larger_than_n() {
        let vectors = vec![vec![1.0, 0.0], vec![0.0, 1.0]];
        let pool: Vec<&[f32]> = vectors.iter().map(|v| v.as_slice()).collect();
        let results = search_vectors(&[1.0, 0.0], &pool, 10);
        assert_eq!(results.len(), 2);
    }

    #[test]
    #[ignore] // Requires live embedding server
    fn test_embed_texts_live() {
        let texts = vec!["test query".to_string()];
        let result = embed_texts(&texts, "http://localhost:48951").unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].len(), 1024);
    }
}
