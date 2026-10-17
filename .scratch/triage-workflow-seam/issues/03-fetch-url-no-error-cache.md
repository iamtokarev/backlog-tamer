# 03 · fetch_url stops caching failures

Status: resolved
Commit: `fix: stop caching failed page fetches`

Spec decision 8. A failed fetch must not be served from the process-wide cache on a warm Lambda.

Done when a `tests/test_fetch_url.py` case shows a failed fetch followed by a successful one for the same URL returns success without `clear_cache()`.

## Comments

Resolved in 9a833ed (PR #35).
