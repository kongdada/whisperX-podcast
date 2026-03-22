# Cache Format

The cache file is `.podcast-transcript-editor-cache.json` stored next to `01_transcript.md`.

Each cache entry is keyed by a deterministic hash of:

- the cleanup prompt id
- the exact source block text

Entries store:

- `block_hash`
- `prompt_id`
- `cleaned_block`
- `source_header`
- `model`
- `updated_at`

Cache reuse rules:

- Reuse only when `block_hash` matches the current source block
- Reuse only when `prompt_id` matches the current cleanup prompt version
- Otherwise fall back to `needs_model`

With the all-turn refinement flow:

- every turn defaults to `needs_model`
- a turn becomes `from_cache` only when the exact same source block already has a cleaned result in cache
- profile replacements applied before planning change the source block text and therefore invalidate stale cache entries automatically

Typical `from_cache` behavior:

- same block content
- same prompt version
- cached cleaned text exists
