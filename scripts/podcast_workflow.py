#!/usr/bin/env python3
"""Podcast download + WhisperX transcription workflow."""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_PROFILE_DIR = "./scripts/podcast_profiles"
DEFAULT_OUT_ROOT = "./outputs"
DEFAULT_MODEL = "large-v3"
DEFAULT_LANGUAGE = "zh"
DEFAULT_BATCH_SIZE = 8
DEFAULT_CHUNK_SIZE = 30
DEFAULT_VAD_METHOD = "pyannote"
DEFAULT_DIARIZE_MODEL = "pyannote/speaker-diarization-community-1"
CAFFEINATE_ENV_FLAG = "PODCAST_WORKFLOW_CAFFEINATED"


class WorkflowError(RuntimeError):
    """Expected workflow failure."""


@dataclass
class EpisodeCandidate:
    title: str
    source_url: str
    playlist_index: int | None
    release_ts: int
    duration_s: int | None
    uploader: str | None


@dataclass
class Segment:
    t0_ms: int
    t1_ms: int
    text: str
    speaker: str | None = None


@dataclass
class SpeakerTurn:
    t0_ms: int
    t1_ms: int
    speaker: str | None
    parts: list[str]


@dataclass
class PodcastProfile:
    name: str
    path: Path
    source_url_regex: str | None
    title_regex: str | None
    input_url_regex: str | None
    speaker_a_name: str | None
    speaker_b_name: str | None
    speaker_name_map: dict[str, str]
    noise_phrases: list[str]
    replacements: dict[str, str]


@dataclass
class WhisperXRunResult:
    transcript_result: dict[str, Any]
    diarization_records: list[dict[str, Any]]
    language: str


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-shot podcast download + WhisperX transcription workflow")
    parser.add_argument("--url", required=True, help="Podcast episode/show URL (Apple Podcasts or Xiaoyuzhou)")
    parser.add_argument(
        "--episode-index",
        type=int,
        default=None,
        help="Select episode index (1-based) from recent-10 list when URL is a show page",
    )
    parser.add_argument("--out-root", default=DEFAULT_OUT_ROOT, help="Output root directory")
    parser.add_argument("--profile", default=None, help="Optional profile name or JSON file path")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="Profile directory")
    parser.add_argument("--speaker-a-name", default=None, help="Optional display name for the first speaker")
    parser.add_argument("--speaker-b-name", default=None, help="Optional display name for the second speaker")
    parser.add_argument(
        "--speaker-name-map",
        action="append",
        default=[],
        help="Explicit speaker mapping, format LABEL=NAME; can be passed multiple times",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="WhisperX model name")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="Transcription language code")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="WhisperX batch size")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="VAD chunk size")
    parser.add_argument("--device", default=None, help="WhisperX device (default: cuda if available else cpu)")
    parser.add_argument("--device-index", type=int, default=0, help="Device index for FasterWhisper inference")
    parser.add_argument(
        "--compute-type",
        default="default",
        choices=["default", "float16", "float32", "int8"],
        help="WhisperX compute type",
    )
    parser.add_argument("--model-dir", default=None, help="Optional download/cache directory for models")
    parser.add_argument(
        "--model-cache-only",
        action="store_true",
        help="Use only locally cached models; fail instead of downloading",
    )
    parser.add_argument("--align-model", default=None, help="Optional phoneme alignment model name")
    parser.add_argument(
        "--vad-method",
        default=DEFAULT_VAD_METHOD,
        choices=["pyannote", "silero"],
        help="VAD method for WhisperX",
    )
    parser.add_argument("--min-speakers", type=int, default=None, help="Minimum number of speakers")
    parser.add_argument("--max-speakers", type=int, default=None, help="Maximum number of speakers")
    parser.add_argument("--hf-token", default=None, help="Hugging Face token for pyannote gated model access")
    parser.add_argument("--diarize-model", default=DEFAULT_DIARIZE_MODEL, help="Pyannote diarization model name")
    parser.add_argument(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        default=(sys.platform == "darwin"),
        help="Prevent system sleep during the workflow on macOS by wrapping the process with caffeinate",
    )
    parser.add_argument("--verbose", action=argparse.BooleanOptionalAction, default=True, help="Verbose WhisperX output")
    parser.add_argument(
        "--print-progress",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Print progress in WhisperX internals",
    )
    parser.add_argument("--retries", type=int, default=1, help="Retries for yt-dlp/ffmpeg transient failures")
    return parser


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", text.strip())
    clean = re.sub(r"-+", "-", clean).strip("-")
    return clean or "episode"


def log(msg: str) -> None:
    print(f"[podcast-workflow] {msg}", flush=True)


def resolve_executable(name_or_path: str) -> str | None:
    candidate = Path(name_or_path)
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(name_or_path)


def run_cmd(
    cmd: list[str],
    *,
    retries: int = 0,
    retry_wait_s: float = 1.0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    last_err: WorkflowError | None = None
    for attempt in range(retries + 1):
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            cwd=str(cwd) if cwd else None,
        )
        if proc.returncode == 0:
            return proc

        last_err = WorkflowError(
            f"command failed (attempt {attempt + 1}/{retries + 1}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
        if attempt < retries:
            import time

            time.sleep(retry_wait_s * (attempt + 1))

    raise last_err or WorkflowError("unknown command failure")


def parse_json_from_maybe_noisy_stdout(raw: str) -> dict[str, Any]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise WorkflowError("failed to parse JSON from yt-dlp output")


def parse_release_ts(entry: dict[str, Any]) -> int:
    for key in ("release_timestamp", "timestamp"):
        val = entry.get(key)
        if isinstance(val, int):
            return val

    upload_date = entry.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        try:
            return int(datetime.strptime(upload_date, "%Y%m%d").timestamp())
        except ValueError:
            return 0
    return 0


def inspect_source(yt_dlp_bin: str, url: str, retries: int) -> dict[str, Any]:
    cmd = [yt_dlp_bin, "--dump-single-json", "--skip-download", "--no-warnings", url]
    proc = run_cmd(cmd, retries=retries)
    return parse_json_from_maybe_noisy_stdout(proc.stdout)


def classify_source(info: dict[str, Any]) -> str:
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        return "show"
    return "episode"


def extract_candidates_from_show(info: dict[str, Any], source_url: str) -> list[EpisodeCandidate]:
    entries = info.get("entries")
    if not isinstance(entries, list) or not entries:
        raise WorkflowError("show page does not contain playable entries")

    candidates: list[EpisodeCandidate] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue

        title = str(entry.get("title") or f"Episode {idx + 1}")
        ep_url = entry.get("webpage_url") or entry.get("original_url") or source_url
        duration = entry.get("duration")
        playlist_index = entry.get("playlist_index")

        candidates.append(
            EpisodeCandidate(
                title=title,
                source_url=str(ep_url),
                playlist_index=playlist_index if isinstance(playlist_index, int) and playlist_index > 0 else idx + 1,
                release_ts=parse_release_ts(entry),
                duration_s=int(duration) if isinstance(duration, (int, float)) else None,
                uploader=entry.get("uploader") if isinstance(entry.get("uploader"), str) else None,
            )
        )

    candidates.sort(key=lambda c: (c.release_ts, c.playlist_index or 0), reverse=True)
    return candidates


def choose_episode(
    candidates: list[EpisodeCandidate],
    episode_index: int | None,
    *,
    input_fn: Callable[[str], str] = input,
) -> EpisodeCandidate:
    display = candidates[:10]
    if not display:
        raise WorkflowError("no episode candidate found")

    if episode_index is not None:
        if episode_index < 1 or episode_index > len(display):
            raise WorkflowError(f"--episode-index must be in [1, {len(display)}]")
        return display[episode_index - 1]

    if not sys.stdin.isatty():
        raise WorkflowError("show URL requires --episode-index in non-interactive environment")

    log("detected show page; choose one episode from recent list:")
    for i, episode in enumerate(display, start=1):
        release = datetime.fromtimestamp(episode.release_ts).strftime("%Y-%m-%d") if episode.release_ts > 0 else "unknown-date"
        duration = f"{episode.duration_s // 60}m" if episode.duration_s else "unknown-duration"
        log(f"{i}. {episode.title} ({release}, {duration})")

    while True:
        raw = input_fn("请输入要处理的集数序号 [1-10]: ").strip()
        if not raw.isdigit():
            print("请输入数字。", flush=True)
            continue
        picked = int(raw)
        if 1 <= picked <= len(display):
            return display[picked - 1]
        print(f"请输入 1 到 {len(display)} 之间的数字。", flush=True)


def discover_profile_files(profile_dir: Path) -> list[Path]:
    if not profile_dir.exists() or not profile_dir.is_dir():
        return []
    return sorted(
        path for path in profile_dir.glob("*.profile.json") if path.is_file() and not path.name.startswith("_")
    )


def parse_profile_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def parse_profile_replacements(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(repl) for key, repl in value.items() if isinstance(key, str) and key and isinstance(repl, str)}


def parse_name_mapping_items(items: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise WorkflowError(f"invalid speaker mapping, expected LABEL=NAME: {item}")
        label, name = item.split("=", 1)
        label = label.strip()
        name = name.strip()
        if not label or not name:
            raise WorkflowError(f"invalid speaker mapping, expected LABEL=NAME: {item}")
        mapping[label] = name
    return mapping


def load_profile_from_file(path: Path) -> PodcastProfile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"failed to load profile {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise WorkflowError(f"profile must be a JSON object: {path}")

    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name") else path.stem
    speaker_name_map = parse_profile_replacements(raw.get("speaker_name_map"))

    return PodcastProfile(
        name=name,
        path=path,
        source_url_regex=match.get("source_url_regex") if isinstance(match.get("source_url_regex"), str) else None,
        title_regex=match.get("title_regex") if isinstance(match.get("title_regex"), str) else None,
        input_url_regex=match.get("input_url_regex") if isinstance(match.get("input_url_regex"), str) else None,
        speaker_a_name=raw.get("speaker_a_name") if isinstance(raw.get("speaker_a_name"), str) else None,
        speaker_b_name=raw.get("speaker_b_name") if isinstance(raw.get("speaker_b_name"), str) else None,
        speaker_name_map=speaker_name_map,
        noise_phrases=parse_profile_text_list(raw.get("noise_phrases")),
        replacements=parse_profile_replacements(raw.get("replacements")),
    )


def regex_matches(pattern: str | None, text: str) -> bool:
    if not pattern:
        return True
    return re.search(pattern, text) is not None


def profile_matches(profile: PodcastProfile, *, input_url: str, source_url: str, title: str) -> bool:
    has_matcher = any([profile.input_url_regex, profile.source_url_regex, profile.title_regex])
    if not has_matcher:
        return False
    return (
        regex_matches(profile.input_url_regex, input_url)
        and regex_matches(profile.source_url_regex, source_url)
        and regex_matches(profile.title_regex, title)
    )


def resolve_profile(
    *,
    explicit_profile: str | None,
    profile_dir: str,
    input_url: str,
    selected_episode: EpisodeCandidate,
) -> PodcastProfile | None:
    profile_root = Path(profile_dir)

    if explicit_profile:
        candidate = Path(explicit_profile)
        if not candidate.exists():
            candidate = (
                profile_root / explicit_profile
                if explicit_profile.endswith(".json")
                else profile_root / f"{explicit_profile}.profile.json"
            )
        if not candidate.exists():
            raise WorkflowError(f"profile not found: {explicit_profile}")
        profile = load_profile_from_file(candidate)
        log(f"using explicit profile: {profile.name} ({candidate})")
        return profile

    for path in discover_profile_files(profile_root):
        profile = load_profile_from_file(path)
        if profile_matches(
            profile,
            input_url=input_url,
            source_url=selected_episode.source_url,
            title=selected_episode.title,
        ):
            log(f"matched profile: {profile.name}")
            return profile
    return None


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    rendered = text
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)
    return rendered


def clone_segments(segments: list[Segment]) -> list[Segment]:
    return [Segment(t0_ms=seg.t0_ms, t1_ms=seg.t1_ms, text=seg.text, speaker=seg.speaker) for seg in segments]


def apply_profile_to_segments(segments: list[Segment], profile: PodcastProfile | None) -> list[Segment]:
    if not profile:
        return segments

    result: list[Segment] = []
    for seg in clone_segments(segments):
        if profile.noise_phrases and any(phrase in seg.text for phrase in profile.noise_phrases):
            continue
        seg.text = apply_replacements(seg.text, profile.replacements)
        result.append(seg)
    return result


def build_speaker_name_map(args: argparse.Namespace, profile: PodcastProfile | None) -> dict[str, str]:
    mapping: dict[str, str] = {}

    if profile:
        mapping.update(profile.speaker_name_map)
        if profile.speaker_a_name:
            mapping.setdefault("SPEAKER_00", profile.speaker_a_name)
            mapping.setdefault("Speaker A", profile.speaker_a_name)
        if profile.speaker_b_name:
            mapping.setdefault("SPEAKER_01", profile.speaker_b_name)
            mapping.setdefault("Speaker B", profile.speaker_b_name)

    if args.speaker_a_name:
        mapping["SPEAKER_00"] = args.speaker_a_name
        mapping["Speaker A"] = args.speaker_a_name
    if args.speaker_b_name:
        mapping["SPEAKER_01"] = args.speaker_b_name
        mapping["Speaker B"] = args.speaker_b_name

    mapping.update(parse_name_mapping_items(args.speaker_name_map))
    return mapping


def normalize_hf_token(token: str | None) -> str | None:
    if token and token.strip():
        return token.strip()
    for env_name in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = os.getenv(env_name)
        if value and value.strip():
            return value.strip()
    return None


def verify_pyannote_access(token: str, model_name: str) -> None:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise WorkflowError("huggingface-hub is not installed; cannot verify pyannote model access") from exc

    try:
        HfApi().model_info(model_name, token=token)
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(
            f"failed to access diarization model '{model_name}'. "
            "Check the HF token and ensure you accepted the pyannote gated model terms."
        ) from exc


def preflight(args: argparse.Namespace) -> tuple[str, str, str]:
    missing: list[str] = []

    yt_dlp_bin = resolve_executable("yt-dlp")
    ffmpeg_bin = resolve_executable("ffmpeg")

    if yt_dlp_bin is None:
        missing.append("yt-dlp")
    if ffmpeg_bin is None:
        missing.append("ffmpeg")

    token = normalize_hf_token(args.hf_token)
    if not token:
        missing.append("hf-token")

    if missing:
        raise WorkflowError(
            "dependency check failed:\n" + "\n".join(f"- missing: {item}" for item in missing)
        )

    assert token is not None
    verify_pyannote_access(token, args.diarize_model)
    return yt_dlp_bin or "yt-dlp", ffmpeg_bin or "ffmpeg", token


def should_keep_awake(args: argparse.Namespace) -> bool:
    return bool(args.keep_awake) and sys.platform == "darwin" and os.getenv(CAFFEINATE_ENV_FLAG) != "1"


def rerun_with_caffeinate() -> int:
    caffeinate_bin = resolve_executable("caffeinate")
    if caffeinate_bin is None:
        log("keep-awake requested but caffeinate is unavailable; continuing without it")
        return -1

    log("keep-awake enabled via caffeinate")
    env = os.environ.copy()
    env[CAFFEINATE_ENV_FLAG] = "1"
    proc = subprocess.run(
        [caffeinate_bin, "-dimsu", sys.executable, *sys.argv],
        env=env,
    )
    return proc.returncode


def find_downloaded_file(stdout: str, temp_dir: Path) -> Path:
    for line in reversed([line.strip() for line in stdout.splitlines()]):
        if not line:
            continue
        candidate = Path(line)
        if candidate.exists() and candidate.is_file():
            return candidate

    files = sorted((path for path in temp_dir.glob("*") if path.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    if files:
        return files[0]
    raise WorkflowError("download finished but no media file found")


def download_audio(
    yt_dlp_bin: str,
    *,
    input_url: str,
    source_type: str,
    selected_episode: EpisodeCandidate,
    temp_dir: Path,
    retries: int,
) -> Path:
    output_tpl = temp_dir / "source.%(ext)s"
    cmd = [
        yt_dlp_bin,
        "-f",
        "bestaudio/best",
        "--no-warnings",
        "--restrict-filenames",
        "--print",
        "after_move:filepath",
        "-o",
        str(output_tpl),
    ]

    if source_type == "show" and selected_episode.playlist_index:
        cmd += ["--yes-playlist", "--playlist-items", str(selected_episode.playlist_index), input_url]
    else:
        cmd += ["--no-playlist", selected_episode.source_url]

    proc = run_cmd(cmd, retries=retries)
    return find_downloaded_file(proc.stdout, temp_dir)


def transcode_to_mp3(ffmpeg_bin: str, src: Path, dst_mp3: Path, retries: int) -> None:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(dst_mp3),
    ]
    run_cmd(cmd, retries=retries)


def default_device() -> str:
    try:
        import torch
    except ImportError:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def clear_torch_cache() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def diarization_records_from_df(diarize_df: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for _, row in diarize_df.iterrows():
        records.append(
            {
                "start": float(row["start"]),
                "end": float(row["end"]),
                "speaker": row["speaker"],
                "label": row["label"],
            }
        )
    return records


def transcribe_audio(args: argparse.Namespace, audio_path: Path, hf_token: str) -> WhisperXRunResult:
    import whisperx
    from whisperx.alignment import align, load_align_model
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    device = args.device or default_device()
    log(f"loading WhisperX model '{args.model}' on {device}")

    model = whisperx.load_model(
        args.model,
        device=device,
        device_index=args.device_index,
        download_root=args.model_dir,
        compute_type=args.compute_type,
        language=args.language,
        vad_method=args.vad_method,
        vad_options={"chunk_size": args.chunk_size},
        task="transcribe",
        local_files_only=args.model_cache_only,
        threads=0,
        use_auth_token=hf_token,
    )

    audio = whisperx.load_audio(str(audio_path))
    result = model.transcribe(
        audio,
        batch_size=args.batch_size,
        chunk_size=args.chunk_size,
        print_progress=args.print_progress,
        verbose=args.verbose,
    )
    del model
    gc.collect()
    clear_torch_cache()

    detected_language = str(result.get("language") or args.language).lower()

    align_model, align_metadata = load_align_model(
        detected_language,
        device,
        model_name=args.align_model,
        model_dir=args.model_dir,
        model_cache_only=args.model_cache_only,
    )
    if align_model is not None and result.get("segments"):
        log(f"aligning transcript with language model '{align_metadata['language']}'")
        result = align(
            result["segments"],
            align_model,
            align_metadata,
            audio,
            device,
            return_char_alignments=False,
            print_progress=args.print_progress,
        )
    del align_model
    gc.collect()
    clear_torch_cache()

    log(f"running diarization with '{args.diarize_model}'")
    diarize_model = DiarizationPipeline(
        model_name=args.diarize_model,
        token=hf_token,
        device=device,
        cache_dir=args.model_dir,
    )
    diarize_df = diarize_model(
        str(audio_path),
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
    )
    result = assign_word_speakers(diarize_df, result, fill_nearest=False)

    diarization_records = diarization_records_from_df(diarize_df)
    result["language"] = detected_language
    return WhisperXRunResult(
        transcript_result=result,
        diarization_records=diarization_records,
        language=detected_language,
    )


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8")


def segments_from_whisperx_result(result: dict[str, Any]) -> list[Segment]:
    rendered: list[Segment] = []
    for item in result.get("segments", []):
        if not isinstance(item, dict):
            continue
        start = item.get("start")
        end = item.get("end")
        text = str(item.get("text") or "").strip()
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not text:
            continue
        speaker = item.get("speaker") if isinstance(item.get("speaker"), str) else None
        rendered.append(
            Segment(
                t0_ms=int(round(float(start) * 1000)),
                t1_ms=int(round(float(end) * 1000)),
                text=text,
                speaker=speaker,
            )
        )
    return rendered


def fmt_ms(ms: int) -> str:
    seconds = max(0, ms // 1000)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


TURN_END_PUNCT = "。！？!?…"
TURN_INLINE_PUNCT = "，；：、,;:"
TURN_LEADING_PUNCT = "，。！？；：、,.!?;:)]）】〉》」』”’"
TURN_WRAP_CHARS = 100


def clean_turn_fragment(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def render_turn_text(parts: list[str]) -> str:
    rendered = ""
    for raw in parts:
        part = clean_turn_fragment(raw).strip(" ，。；;")
        if not part:
            continue
        if not rendered:
            rendered = part
            continue
        if rendered[-1] in TURN_END_PUNCT + TURN_INLINE_PUNCT or part[0] in TURN_LEADING_PUNCT:
            rendered += part
        else:
            rendered += "，" + part

    rendered = re.sub(r"，{2,}", "，", rendered)
    rendered = re.sub(r"([。！？!?…])，", r"\1", rendered)
    if rendered and rendered[-1] not in TURN_END_PUNCT:
        rendered += "。"
    return rendered


def split_keep_punct(text: str, punctuation: str) -> list[str]:
    pieces: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if ch in punctuation:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces


def hard_wrap_text(text: str, max_chars: int) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    return [clean[i : i + max_chars] for i in range(0, len(clean), max_chars)]


def pack_chunks(chunks: list[str], max_chars: int) -> list[str]:
    lines: list[str] = []
    current = ""

    for raw in chunks:
        chunk = raw.strip()
        if not chunk:
            continue
        if len(chunk) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.extend(hard_wrap_text(chunk, max_chars))
            continue
        if not current:
            current = chunk
            continue
        if len(current) + len(chunk) <= max_chars:
            current += chunk
        else:
            lines.append(current)
            current = chunk

    if current:
        lines.append(current)

    return lines


def wrap_turn_text(text: str, max_chars: int = TURN_WRAP_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    sentence_chunks = split_keep_punct(text, TURN_END_PUNCT)
    if len(sentence_chunks) == 1:
        return pack_chunks(split_keep_punct(text, TURN_INLINE_PUNCT), max_chars)

    lines: list[str] = []
    current = ""
    for sentence in sentence_chunks:
        clause_lines = pack_chunks(split_keep_punct(sentence, TURN_INLINE_PUNCT), max_chars)
        for clause_line in clause_lines:
            if not current:
                current = clause_line
                continue
            if len(current) + len(clause_line) <= max_chars:
                current += clause_line
            else:
                lines.append(current)
                current = clause_line
    if current:
        lines.append(current)
    return lines


def merge_segments_into_turns(segments: list[Segment]) -> list[SpeakerTurn]:
    turns: list[SpeakerTurn] = []
    current: SpeakerTurn | None = None

    for seg in segments:
        text = clean_turn_fragment(seg.text)
        if not text and current is None:
            continue
        if current and current.speaker == seg.speaker:
            current.t1_ms = seg.t1_ms
            if text:
                current.parts.append(text)
            continue
        if current:
            turns.append(current)
        current = SpeakerTurn(
            t0_ms=seg.t0_ms,
            t1_ms=seg.t1_ms,
            speaker=seg.speaker,
            parts=[text] if text else [],
        )

    if current:
        turns.append(current)

    return [turn for turn in turns if turn.parts]


def transcript_markdown(
    segments: list[Segment],
    *,
    source_url: str,
    episode_title: str,
    language: str,
    speaker_names: dict[str, str] | None = None,
) -> str:
    lines = [
        "# 转写稿",
        "",
        f"- 生成时间: {now_iso()}",
        f"- 来源链接: {source_url}",
        f"- 集标题: {episode_title}",
        f"- 识别语言: {language}",
        "",
        "## 正文",
        "",
    ]

    if any(seg.speaker for seg in segments):
        for turn in merge_segments_into_turns(segments):
            speaker = speaker_names.get(turn.speaker, turn.speaker) if speaker_names and turn.speaker else turn.speaker
            ts = f"{fmt_ms(turn.t0_ms)} - {fmt_ms(turn.t1_ms)}"
            text = render_turn_text(turn.parts)
            if speaker:
                lines.append(f"{speaker}（{ts}）：")
            else:
                lines.append(f"（{ts}）：")
            lines.extend(wrap_turn_text(text))
            lines.append("")
    else:
        for seg in segments:
            ts = f"[{fmt_ms(seg.t0_ms)} - {fmt_ms(seg.t1_ms)}]"
            lines.append(f"- {ts} {seg.text}")

    lines.append("")
    return "\n".join(lines)


def execute_workflow(args: argparse.Namespace, *, input_fn: Callable[[str], str] = input) -> Path:
    yt_dlp_bin, ffmpeg_bin, hf_token = preflight(args)

    info = inspect_source(yt_dlp_bin, args.url, args.retries)
    source_type = classify_source(info)

    if source_type == "show":
        candidates = extract_candidates_from_show(info, args.url)
        selected = choose_episode(candidates, args.episode_index, input_fn=input_fn)
    else:
        selected = EpisodeCandidate(
            title=str(info.get("title") or "Episode"),
            source_url=str(info.get("webpage_url") or info.get("original_url") or args.url),
            playlist_index=None,
            release_ts=parse_release_ts(info),
            duration_s=int(info["duration"]) if isinstance(info.get("duration"), (int, float)) else None,
            uploader=info.get("uploader") if isinstance(info.get("uploader"), str) else None,
        )

    profile = resolve_profile(
        explicit_profile=args.profile,
        profile_dir=args.profile_dir,
        input_url=args.url,
        selected_episode=selected,
    )
    speaker_names = build_speaker_name_map(args, profile)

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(selected.title)[:48]}"
    out_dir = Path(args.out_root).resolve() / run_id
    out_dir.mkdir(parents=True, exist_ok=False)

    with tempfile.TemporaryDirectory(prefix="podcast-workflow-") as tmp_root:
        tmp_dir = Path(tmp_root)

        log("downloading audio source")
        source_audio = download_audio(
            yt_dlp_bin,
            input_url=args.url,
            source_type=source_type,
            selected_episode=selected,
            temp_dir=tmp_dir,
            retries=args.retries,
        )

        audio_mp3 = out_dir / "audio.mp3"
        log("transcoding audio to mp3")
        transcode_to_mp3(ffmpeg_bin, source_audio, audio_mp3, retries=args.retries)

    whisperx_run = transcribe_audio(args, audio_mp3, hf_token)

    transcript_json_path = out_dir / "01_transcript.json"
    diarization_json_path = out_dir / "01_diarization.json"
    save_json(transcript_json_path, whisperx_run.transcript_result)
    save_json(
        diarization_json_path,
        {
            "generated_at": now_iso(),
            "model": args.diarize_model,
            "records": whisperx_run.diarization_records,
        },
    )

    segments = segments_from_whisperx_result(whisperx_run.transcript_result)
    segments = apply_profile_to_segments(segments, profile)
    transcript_md = transcript_markdown(
        segments,
        source_url=args.url,
        episode_title=selected.title,
        language=whisperx_run.language,
        speaker_names=speaker_names,
    )
    transcript_path = out_dir / "01_transcript.md"
    transcript_path.write_text(transcript_md, encoding="utf-8")

    log(f"output directory: {out_dir}")
    log(f"transcript path: {transcript_path}")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if should_keep_awake(args):
        rc = rerun_with_caffeinate()
        if rc >= 0:
            return rc

    try:
        execute_workflow(args)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
