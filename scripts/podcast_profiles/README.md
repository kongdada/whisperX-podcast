# Podcast Profiles

`scripts/podcast_workflow.py` supports optional per-show or per-episode profiles.

Use profiles for deterministic rules only:

- fixed speaker display names
- fixed speaker label mappings
- known noise phrases
- known glossary replacements

Recommended file pattern:

- `*.profile.json`

Files beginning with `_` are ignored by auto-detection.

Matching fields are optional, but auto-detection only works when at least one matcher is provided:

- `input_url_regex`
- `source_url_regex`
- `title_regex`

Example:

```json
{
  "name": "demo",
  "match": {
    "title_regex": "知行小酒馆"
  },
  "speaker_a_name": "雨白",
  "speaker_b_name": "张潇雨",
  "speaker_name_map": {
    "SPEAKER_00": "雨白",
    "SPEAKER_01": "张潇雨"
  },
  "noise_phrases": [
    "请不吝点赞"
  ],
  "replacements": {
    "执行小酒馆": "知行小酒馆"
  }
}
```

Notes:

- `speaker_name_map` is the most precise option for WhisperX labels.
- `speaker_a_name` / `speaker_b_name` are convenience defaults for the first two speakers.
- CLI flags override profile mappings.
