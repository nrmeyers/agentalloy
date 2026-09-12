//! Index-grounded validation: symbol-exists, slug-known, etc.
//! Design §8: fail-open on store error (AC-13).

use anyhow::Result;

/// Validate that a symbol FQN exists in the index.
/// Fail-open: if the index is unavailable, return true (don't block the operation).
#[allow(dead_code)]
pub fn symbol_exists(_fqn: &str) -> Result<bool> {
    // TODO T3: query overgraph for symbol node
    // TODO T3: on error, return Ok(true) (fail-open)
    Ok(true)
}

/// Validate that a contract slug is known.
/// Fail-open: if the store is unavailable, return true.
#[allow(dead_code)]
pub fn slug_known(_slug: &str, _duck_path: &str) -> Result<bool> {
    // TODO T7: query DuckDB RO for contract
    // TODO T7: on error, return Ok(true) (fail-open)
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_symbol_exists_fail_open() {
        // With no index, should fail-open (return true)
        assert!(symbol_exists("nonexistent.Symbol").unwrap());
    }

    #[test]
    fn test_slug_known_fail_open() {
        // With no store, should fail-open (return true)
        assert!(slug_known("nonexistent-slug", "/nonexistent.duck").unwrap());
    }
}
