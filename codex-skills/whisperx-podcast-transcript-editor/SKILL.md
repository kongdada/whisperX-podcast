---
name: whisperx-podcast-transcript-editor
description: Download a Xiaoyuzhou or Apple Podcasts episode with the local WhisperX workflow, then produce a faithful cleaned transcript. Also works from an existing 01_transcript.md.
---

# WhisperX Podcast Transcript Editor

Use this skill when the user wants a podcast URL turned into a faithful cleaned transcript, or when they already have `01_transcript.md` and only need the cleanup stage.

The workflow is intentionally split into two layers:

- `01_transcript.md`: rule-generated structure draft for model consumption and rough human review
- `02_transcript_clean.md`: final high-quality transcript, produced by running the model over every turn block unless cache already has a matching cleaned block

## Quick Start

Treat the request as one of these two cases:

- Podcast URL: run `scripts/podcast_workflow.py` first, then clean the generated structure draft
- Existing transcript path: read `01_transcript.md`, then clean it directly

## URL Workflow

For podcast URLs, run from the repository root:

```bash
python3 scripts/podcast_workflow.py --url "<podcast-url>"
```

Add options only when already known or clearly needed:

- `--episode-index` for show pages in non-interactive runs
- `--profile <name>` when a matching podcast profile exists
- `--speaker-a-name` / `--speaker-b-name` for the common 2-speaker case
- `--speaker-name-map LABEL=NAME` for any explicit WhisperX speaker mapping
- `--min-speakers` / `--max-speakers` when the speaker count is known
- `--hf-token <token>` when the token is not already available in the environment

Do not claim accurate speaker naming unless the mapping is explicit. WhisperX diarization gives speaker labels like `SPEAKER_00`; those labels only become names when the CLI flags or profile define them.

`01_transcript.md` is not the final reader-facing transcript. Its job is to preserve speaker/time structure and keep turns small enough for reliable turn-level LLM editing.

## Cleanup Flow

Always build the cleanup plan before sending anything to the model:

```bash
python3 codex-skills/whisperx-podcast-transcript-editor/scripts/cleanup_helper.py plan \
  "<path-to-01_transcript.md>" \
  --output "<path-to-cleanup-plan.json>"
```

If a matching podcast profile exists, pass it too:

```bash
python3 codex-skills/whisperx-podcast-transcript-editor/scripts/cleanup_helper.py plan \
  "<path-to-01_transcript.md>" \
  --profile-file "scripts/podcast_profiles/<profile>.profile.json" \
  --output "<path-to-cleanup-plan.json>"
```

Before editing, read:

- [references/cleanup-standard.zh.md](references/cleanup-standard.zh.md)
- [references/cleanup-prompt.zh.txt](references/cleanup-prompt.zh.txt)
- [references/cache-format.md](references/cache-format.md)

Send every block where `decision == "needs_model"`.

Skip only blocks where:

- `decision == "from_cache"`

Use the fixed prompt in [references/cleanup-prompt.zh.txt](references/cleanup-prompt.zh.txt) for each dirty block.

## Assemble Output

After the model returns cleaned text for each dirty block, write the cleaned block back into the plan JSON under `cleaned_block`, then assemble:

```bash
python3 codex-skills/whisperx-podcast-transcript-editor/scripts/cleanup_helper.py assemble \
  "<path-to-cleanup-plan.json>" \
  --output "<path-to-02_transcript_clean.md>" \
  --model "<model-name>"
```

This reuses cache hits, writes `02_transcript_clean.md`, and updates `.podcast-transcript-editor-cache.json`.

## Output Rules

Write `02_transcript_clean.md` as the final cleaned transcript, not as notes about the cleanup.

Do:

- Keep the original title and header structure
- Keep speaker labels and time ranges
- Run the model on every turn block unless cache already contains an exact-match cleaned block
- Split long paragraphs where reading becomes tiring
- Add punctuation and sentence boundaries so the text reads like a professionally checked transcript
- Remove obvious noise or duplicated junk only when confidence is high

Do not:

- Summarize
- Add interpretation
- Change claims or positions
- Reassign speakers
- Rewrite into a polished article
