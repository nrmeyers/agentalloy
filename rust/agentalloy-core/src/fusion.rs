//! RRF (Reciprocal Rank Fusion) for combining dense + lexical results.
//! Design §8: deterministic RRF fusion → top-k.

/// Reciprocal Rank Fusion: combine two ranked lists into a single ranked list.
/// RRF score = sum(1 / (k + rank_i)) for each list i, where k=60 (standard).
#[allow(dead_code)]
pub fn rrf_fusion(dense_results: &[usize], lexical_results: &[usize], k: usize) -> Vec<usize> {
    use std::collections::HashMap;

    const RRF_K: usize = 60;

    let mut scores: HashMap<usize, f64> = HashMap::new();

    // Score from dense results
    for (rank, &doc_id) in dense_results.iter().enumerate() {
        *scores.entry(doc_id).or_insert(0.0) += 1.0 / (RRF_K + rank + 1) as f64;
    }

    // Score from lexical results
    for (rank, &doc_id) in lexical_results.iter().enumerate() {
        *scores.entry(doc_id).or_insert(0.0) += 1.0 / (RRF_K + rank + 1) as f64;
    }

    // Sort by score descending; doc_id breaks ties so equal-score docs come
    // back in the same order every run (HashMap iteration is randomized).
    // total_cmp: never panic on NaN.
    let mut ranked: Vec<(usize, f64)> = scores.into_iter().collect();
    ranked.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
    ranked.into_iter().take(k).map(|(doc_id, _)| doc_id).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rrf_empty_lists() {
        let result = rrf_fusion(&[], &[], 10);
        assert!(result.is_empty());
    }

    #[test]
    fn test_rrf_single_list() {
        let result = rrf_fusion(&[0, 1, 2], &[], 10);
        assert_eq!(result, vec![0, 1, 2]);
    }

    #[test]
    fn test_rrf_overlap() {
        // Doc 1 appears in both lists → highest RRF score
        let result = rrf_fusion(&[0, 1, 2], &[1, 3, 4], 10);
        assert_eq!(result[0], 1); // doc 1 should be first
    }

    #[test]
    fn test_rrf_top_k() {
        let result = rrf_fusion(&[0, 1, 2, 3, 4], &[5, 6, 7, 8, 9], 3);
        assert_eq!(result.len(), 3);
    }
}
