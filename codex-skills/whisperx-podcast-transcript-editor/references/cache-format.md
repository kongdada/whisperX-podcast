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
- Otherwise fall back to `needs_model` or `pass_through`

Typical `needs_model` reasons include:

- obvious punctuation gaps
- repeated junk fragments
- profile-defined replacement hits
- noise phrase hits
- long unbroken blocks

Typical `pass_through` reasons include:

- block already reads cleanly
- no profile replacement hits
- no suspicious markers

Typical `from_cache` behavior:

- same block content
- same prompt version
- cached cleaned text exists
