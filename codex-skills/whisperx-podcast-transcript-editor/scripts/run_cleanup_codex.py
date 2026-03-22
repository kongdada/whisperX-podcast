#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cleanup_helper as ch


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_RETRIES = 3


def default_prompt_path() -> Path:
    return SCRIPT_DIR.parent / "references" / "cleanup-prompt.zh.txt"


def load_prompt(prompt_path: Path | None = None) -> str:
    source = prompt_path or default_prompt_path()
    return source.read_text(encoding="utf-8").strip()


def build_block_prompt(prompt_text: str, source_block: str) -> str:
    return f"{prompt_text}\n\n下面是要处理的 block：\n\n{source_block}\n"


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
    cleaned = block.get("cleaned_block")
    if isinstance(cleaned, str) and cleaned.strip():
        return False
    return True


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
    assemble: bool = True,
    fallback_source: bool = False,
) -> dict[str, int]:
    plan = ch.load_json(plan_path)
    blocks = plan.get("blocks") if isinstance(plan.get("blocks"), list) else []
    prompt_text = load_prompt(prompt_path)
    total = len(blocks)
    completed = sum(1 for block in blocks if not block_needs_cleanup(block))
    processed = 0

    for block in blocks:
        index = block.get("index")
        if not isinstance(index, int):
            continue
        if start_index is not None and index < start_index:
            continue
        if end_index is not None and index > end_index:
            continue
        if limit is not None and processed >= limit:
            break
        if not block_needs_cleanup(block):
            continue

        source_block = block.get("source_block") if isinstance(block.get("source_block"), str) else ""
        header_line = block.get("header_line") if isinstance(block.get("header_line"), str) else ""
        if not source_block or not header_line:
            raise ValueError(f"plan block {index} is missing source_block or header_line")

        prompt = build_block_prompt(prompt_text, source_block)
        success = False
        for attempt in range(1, retries + 1):
            temp_output = Path(tempfile.gettempdir()) / f"codex-clean-block-{index}.txt"
            if temp_output.exists():
                temp_output.unlink()
            started_at = time.time()
            try:
                raw_output = run_codex_exec(prompt, model=model, output_path=temp_output)
                cleaned_block = normalize_cleaned_block(raw_output, header_line)
            except Exception as exc:
                elapsed = time.time() - started_at
                print(f"[{completed}/{total}] block={index} failed attempt={attempt} secs={elapsed:.1f}", flush=True)
                if attempt >= retries:
                    raise RuntimeError(f"cleanup failed for block {index}") from exc
                time.sleep(min(10 * attempt, 30))
                continue

            elapsed = time.time() - started_at
            block["cleaned_block"] = cleaned_block
            save_plan_atomic(plan_path, plan)
            completed += 1
            processed += 1
            print(f"[{completed}/{total}] block={index} ok attempt={attempt} secs={elapsed:.1f}", flush=True)
            success = True
            break

        if not success:
            break

    assembled = 0
    if assemble:
        if output_path is None:
            transcript_path = Path(plan.get("transcript_path")) if isinstance(plan.get("transcript_path"), str) else plan_path
            output_path = transcript_path.parent / "02_transcript_clean.md"
        ch.assemble_from_plan(plan_path, output_path, model=model, fallback_source=fallback_source)
        assembled = 1

    return {"total": total, "completed": completed, "processed": processed, "assembled": assembled}


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
        f"done total={stats['total']} completed={stats['completed']} processed={stats['processed']} assembled={stats['assembled']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
