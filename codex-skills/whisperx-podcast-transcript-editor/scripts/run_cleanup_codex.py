#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cleanup_helper as ch


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_RETRIES = 3
DEFAULT_MAX_BLOCKS_PER_BATCH = 3
DEFAULT_MAX_BATCH_CHARS = 700


@dataclass
class CleanupBatch:
    blocks: list[dict[str, Any]]

    @property
    def indexes(self) -> list[int]:
        return [int(block["index"]) for block in self.blocks]

    @property
    def total_chars(self) -> int:
        return sum(len(str(block.get("source_block", ""))) for block in self.blocks)


def default_prompt_path() -> Path:
    return SCRIPT_DIR.parent / "references" / "cleanup-batch-prompt.zh.txt"


def load_prompt(prompt_path: Path | None = None) -> str:
    source = prompt_path or default_prompt_path()
    return source.read_text(encoding="utf-8").strip()


def build_batch_prompt(prompt_text: str, batch: CleanupBatch) -> str:
    payload = {
        "blocks": [
            {
                "index": int(block["index"]),
                "source_block": str(block["source_block"]),
            }
            for block in batch.blocks
        ]
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"{prompt_text}\n\n下面是要处理的 blocks JSON：\n\n{rendered}\n"


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def normalize_cleaned_block(raw_output: str, expected_header: str) -> str:
    text = raw_output.strip()
    if not text:
        raise ValueError("empty model output")
    if not text.startswith(expected_header):
        index = text.find(expected_header)
        if index >= 0:
            text = text[index:].strip()
    if not text.startswith(expected_header):
        raise ValueError("model output does not preserve the expected header line")
    return text


def extract_json_object(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(candidate):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(candidate[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("model output does not contain a valid JSON object")


def parse_batch_response(raw_output: str, batch: CleanupBatch) -> list[tuple[int, str]]:
    payload = extract_json_object(raw_output)
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ValueError("model output JSON must contain a 'blocks' list")
    if len(raw_blocks) != len(batch.blocks):
        raise ValueError("model output block count does not match request")

    expected_indexes = batch.indexes
    cleaned_items: list[tuple[int, str]] = []
    for expected_block, raw_item, expected_index in zip(batch.blocks, raw_blocks, expected_indexes):
        if not isinstance(raw_item, dict):
            raise ValueError("model output contains a non-object block entry")
        index = raw_item.get("index")
        cleaned_block = raw_item.get("cleaned_block")
        if index != expected_index:
            raise ValueError("model output block indexes do not match request order")
        if not isinstance(cleaned_block, str):
            raise ValueError("model output block is missing cleaned_block")
        normalized = normalize_cleaned_block(cleaned_block, str(expected_block["header_line"]))
        cleaned_items.append((expected_index, normalized))
    return cleaned_items


def run_codex_exec(prompt: str, *, model: str, output_path: Path) -> str:
    home = codex_home()
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-m",
        model,
        "-s",
        "workspace-write",
        "--add-dir",
        str(home),
        "--color",
        "never",
        "-o",
        str(output_path),
        "-",
    ]
    result = subprocess.run(
        cmd,
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        tail = result.stdout.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(tail or f"codex exec failed with exit code {result.returncode}")
    rendered = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
    if not rendered:
        raise RuntimeError("codex exec returned success but produced an empty output file")
    return rendered


def save_plan_atomic(plan_path: Path, plan: dict[str, Any]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        encoding="utf-8",
        dir=str(plan_path.parent),
        prefix=f"{plan_path.name}.tmp-",
    ) as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(plan_path)


def block_needs_cleanup(block: dict[str, Any]) -> bool:
    if block.get("decision") != "needs_model":
        return False
    cleaned = block.get("cleaned_block")
    if isinstance(cleaned, str) and cleaned.strip():
        return False
    return True


def block_is_final(block: dict[str, Any]) -> bool:
    decision = block.get("decision")
    if decision in {"from_cache", "pass_through"}:
        return True
    return not block_needs_cleanup(block)


def next_batch(
    blocks: list[dict[str, Any]],
    *,
    start_pos: int,
    max_blocks: int,
    max_chars: int,
) -> CleanupBatch | None:
    selected: list[dict[str, Any]] = []
    current_chars = 0
    for pos in range(start_pos, len(blocks)):
        block = blocks[pos]
        if not block_needs_cleanup(block):
            continue
        source_block = str(block.get("source_block", ""))
        block_chars = len(source_block)
        if selected and (len(selected) >= max_blocks or current_chars + block_chars > max_chars):
            break
        selected.append(block)
        current_chars += block_chars
        if len(selected) >= max_blocks or current_chars >= max_chars:
            break
    if not selected:
        return None
    return CleanupBatch(blocks=selected)


def run_cleanup_plan(
    plan_path: Path,
    *,
    output_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    retries: int = DEFAULT_RETRIES,
    prompt_path: Path | None = None,
    start_index: int | None = None,
    end_index: int | None = None,
    limit: int | None = None,
    max_blocks_per_batch: int = DEFAULT_MAX_BLOCKS_PER_BATCH,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
    assemble: bool = True,
    fallback_source: bool = False,
) -> dict[str, int]:
    plan = ch.load_json(plan_path)
    blocks = plan.get("blocks") if isinstance(plan.get("blocks"), list) else []
    prompt_text = load_prompt(prompt_path)
    total = len(blocks)
    completed = sum(1 for block in blocks if block_is_final(block))
    processed = 0
    batch_requests = 0
    model_candidates = 0

    filtered_blocks: list[dict[str, Any]] = []
    for block in blocks:
        index = block.get("index")
        if not isinstance(index, int):
            continue
        if start_index is not None and index < start_index:
            continue
        if end_index is not None and index > end_index:
            continue
        filtered_blocks.append(block)

    pos = 0
    while pos < len(filtered_blocks):
        batch = next_batch(
            filtered_blocks,
            start_pos=pos,
            max_blocks=max_blocks_per_batch,
            max_chars=max_batch_chars,
        )
        if batch is None:
            break
        if limit is not None and processed >= limit:
            break
        if limit is not None and processed + len(batch.blocks) > limit:
            trimmed_blocks = batch.blocks[: max(limit - processed, 0)]
            if not trimmed_blocks:
                break
            batch = CleanupBatch(blocks=trimmed_blocks)

        prompt = build_batch_prompt(prompt_text, batch)
        success = False
        for attempt in range(1, retries + 1):
            temp_output = Path(tempfile.gettempdir()) / f"codex-clean-batch-{batch.indexes[0]}-{batch.indexes[-1]}.txt"
            if temp_output.exists():
                temp_output.unlink()
            started_at = time.time()
            try:
                raw_output = run_codex_exec(prompt, model=model, output_path=temp_output)
                cleaned_items = parse_batch_response(raw_output, batch)
            except Exception as exc:
                elapsed = time.time() - started_at
                print(
                    f"[{completed}/{total}] batch={batch.indexes[0]}-{batch.indexes[-1]} failed attempt={attempt} secs={elapsed:.1f}",
                    flush=True,
                )
                if attempt >= retries:
                    raise RuntimeError(f"cleanup failed for batch {batch.indexes[0]}-{batch.indexes[-1]}") from exc
                time.sleep(min(10 * attempt, 30))
                continue

            elapsed = time.time() - started_at
            for block, (_, cleaned_block) in zip(batch.blocks, cleaned_items):
                block["cleaned_block"] = cleaned_block
            save_plan_atomic(plan_path, plan)
            completed += len(batch.blocks)
            processed += len(batch.blocks)
            batch_requests += 1
            model_candidates += len(batch.blocks)
            print(
                f"[{completed}/{total}] batch={batch.indexes[0]}-{batch.indexes[-1]} blocks={len(batch.blocks)} ok attempt={attempt} secs={elapsed:.1f}",
                flush=True,
            )
            success = True
            break

        if not success:
            break
        pos = filtered_blocks.index(batch.blocks[-1]) + 1

    assembled = 0
    if assemble:
        if output_path is None:
            transcript_path = Path(plan.get("transcript_path")) if isinstance(plan.get("transcript_path"), str) else plan_path
            output_path = transcript_path.parent / "02_transcript_clean.md"
        ch.assemble_from_plan(plan_path, output_path, model=model, fallback_source=fallback_source)
        assembled = 1

    return {
        "total": total,
        "completed": completed,
        "processed": processed,
        "assembled": assembled,
        "batch_requests": batch_requests,
        "model_candidates": model_candidates,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run turn-level transcript cleanup with codex exec")
    parser.add_argument("plan", help="Path to cleanup plan JSON")
    parser.add_argument("--output", default=None, help="Output path for 02_transcript_clean.md")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Codex model to use for per-block cleanup")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries per block on model failure")
    parser.add_argument("--prompt-file", default=None, help="Optional cleanup prompt file override")
    parser.add_argument("--start-index", type=int, default=None, help="Only process blocks with index >= this value")
    parser.add_argument("--end-index", type=int, default=None, help="Only process blocks with index <= this value")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of unfinished blocks to process this run")
    parser.add_argument("--max-blocks-per-batch", type=int, default=DEFAULT_MAX_BLOCKS_PER_BATCH, help="Maximum needs_model blocks per model request")
    parser.add_argument("--max-batch-chars", type=int, default=DEFAULT_MAX_BATCH_CHARS, help="Maximum combined source_block characters per model request")
    parser.add_argument("--no-assemble", action="store_true", help="Skip final assemble after block cleanup")
    parser.add_argument(
        "--fallback-source",
        action="store_true",
        help="When assembling, use source_block for unfinished entries instead of failing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    plan_path = ch.require_file(args.plan).resolve()
    output_path = Path(args.output).resolve() if args.output else None
    prompt_path = ch.require_file(args.prompt_file).resolve() if args.prompt_file else None

    try:
        stats = run_cleanup_plan(
            plan_path,
            output_path=output_path,
            model=args.model,
            retries=max(args.retries, 1),
            prompt_path=prompt_path,
            start_index=args.start_index,
            end_index=args.end_index,
            limit=args.limit,
            max_blocks_per_batch=max(args.max_blocks_per_batch, 1),
            max_batch_chars=max(args.max_batch_chars, 1),
            assemble=not args.no_assemble,
            fallback_source=args.fallback_source,
        )
    except KeyboardInterrupt:
        print("interrupted; rerun the same command to resume from the saved plan", file=sys.stderr)
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        "done "
        f"total={stats['total']} completed={stats['completed']} processed={stats['processed']} "
        f"batch_requests={stats['batch_requests']} model_candidates={stats['model_candidates']} assembled={stats['assembled']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
