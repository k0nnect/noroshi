# Corrections to the resolution pass files

## 2026-11-16: pydicom

The `domain` value of the `pydicom` line was changed from `github.io` to `pydicom.github.io`. The change is in `icsma_memory_a.jsonl` (line 61, model `claude-sonnet-5-5`) and `icsma_memory_b.jsonl` (line 61, model `claude-opus-5-5`). Nothing else in either file was changed.

Why: each model's answer was cleaned to a registrable domain when the pass files were written. The suffix list used then did not treat `github.io` as a suffix, so `pydicom.github.io` was cut to `github.io`. That is GitHub's own site, not the pydicom project's. The suffix list now counts private suffixes such as `github.io`, so `pydicom.github.io` is its own registrable domain.

Evidence: both memory replies for the name `pydicom` (item 11 of their batch) are logged in `data/raw/llm_log.jsonl`:

- Line 7, `started_at` 2026-11-16T05:25:32+00:00, model `claude-sonnet-5-5`, purpose `resolve_memory`, status `ok`. The reply contains `{"n":11,"domain":"pydicom.github.io"}`.
- Line 28, `started_at` 2026-11-16T05:25:57+00:00, model `claude-opus-5-5`, purpose `resolve_memory`, status `ok`. The reply contains `{"n": 11, "domain": "pydicom.github.io"}`.

No model was called again. The icsma domain table was then rebuilt from the pass files with no download and no model call.
