"""Token counting for the steering proxy.

Counts tokens using the model server's own /tokenize endpoint (exact),
with tiktoken and char-estimate fallbacks for when the model server
doesn't expose /tokenize or is unreachable.
"""

from __future__ import annotations

import httpx


class TokenCounter:
    """Count tokens with cascading fallback: model server → tiktoken → char estimate."""

    def __init__(
        self,
        model_url: str = "http://localhost:50001",
        api_key: str = "",
        fallback_encoding: str = "cl100k_base",
    ) -> None:
        self.model_url = model_url.rstrip("/")
        self.api_key = api_key
        self.fallback_encoding = fallback_encoding
        self._tiktoken_enc: object | None = None
        self._tiktoken_tried = False

    def count(self, text: str) -> int:
        """Count tokens in text. Tries /tokenize first, then tiktoken, then char estimate."""
        if not text:
            return 0

        result = self._try_model_server(text)
        if result is not None:
            return result

        result = self._try_tiktoken(text)
        if result is not None:
            return result

        return self._char_estimate(text)

    def _try_model_server(self, text: str) -> int | None:
        """Use the model server's /tokenize endpoint for exact counts."""
        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            resp = httpx.post(
                f"{self.model_url}/tokenize",
                json={"content": text},
                headers=headers,
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                tokens = data.get("tokens")
                if isinstance(tokens, list):
                    return len(tokens)
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        return None

    def _try_tiktoken(self, text: str) -> int | None:
        """Use tiktoken as a fallback tokenizer."""
        if not self._tiktoken_tried:
            self._tiktoken_tried = True
            try:
                import tiktoken

                self._tiktoken_enc = tiktoken.get_encoding(self.fallback_encoding)
            except (ImportError, ValueError):
                self._tiktoken_enc = None

        if self._tiktoken_enc is not None:
            try:
                enc = self._tiktoken_enc
                return len(enc.encode(text))  # type: ignore[attr-defined]
            except Exception:
                pass
        return None

    @staticmethod
    def _char_estimate(text: str) -> int:
        """Rough estimate: ~4 chars per token for English text."""
        return max(1, len(text) // 4)
