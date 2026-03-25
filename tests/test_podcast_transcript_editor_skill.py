import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "codex-skills" / "whisperx-podcast-transcript-editor" / "scripts" / "cleanup_helper.py"
SPEC = importlib.util.spec_from_file_location("cleanup_helper", MODULE_PATH)
ch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = ch
SPEC.loader.exec_module(ch)

RUNNER_PATH = REPO_ROOT / "codex-skills" / "whisperx-podcast-transcript-editor" / "scripts" / "run_cleanup_codex.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("run_cleanup_codex", RUNNER_PATH)
runner = importlib.util.module_from_spec(RUNNER_SPEC)
assert RUNNER_SPEC and RUNNER_SPEC.loader
sys.modules[RUNNER_SPEC.name] = runner
RUNNER_SPEC.loader.exec_module(runner)


TRANSCRIPT = (
    "# 转写稿\n\n"
    "- 生成时间: now\n"
    "- 来源链接: https://example.com\n"
    "- 集标题: 示例\n"
    "- 识别语言: zh\n\n"
    "## 正文\n\n"
    "张潇雨（00:00:00 - 00:00:10）：\n"
    "这是一段已经很干净的表达。\n\n"
    "雨白（00:00:10 - 00:00:40）：\n"
    "这个事情就是就是就是特别重要因为如果你不这么看你就很难理解整个结构它其实不是股价不是stock price而是那个。\n"
)


class CleanupHelperTests(unittest.TestCase):
    def test_analyze_clean_block_passes_through(self) -> None:
        block = "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。"
        result = ch.analyze_block(block)
        self.assertEqual(result["decision"], "pass_through")
        self.assertIn("clean_enough", result["reasons"])

    def test_analyze_dirty_block_needs_model(self) -> None:
        block = (
            "雨白（00:00:10 - 00:00:40）：\n"
            "这个事情就是就是就是特别重要因为如果你不这么看你就很难理解整个结构它其实不是股价不是stock price而是那个"
        )
        result = ch.analyze_block(block)
        self.assertEqual(result["decision"], "needs_model")
        self.assertGreaterEqual(result["dirty_score"], 2)
        self.assertTrue(any(reason in result["reasons"] for reason in ["repeated_phrase", "long_without_sentence_end"]))

    def test_build_plan_uses_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_path = tmp / "01_transcript.md"
            transcript_path.write_text(TRANSCRIPT, encoding="utf-8")

            header, blocks = ch.split_transcript_markdown(TRANSCRIPT)
            dirty_block = blocks[1]
            key = ch.cache_key_for_block(dirty_block)
            cache_path = tmp / ch.DEFAULT_CACHE_FILENAME
            cache_data = {
                "version": ch.CACHE_VERSION,
                "prompt_id": ch.PROMPT_ID,
                "created_at": "now",
                "updated_at": "now",
                "entries": {
                    key: {
                        "block_hash": ch.block_hash(dirty_block),
                        "prompt_id": ch.PROMPT_ID,
                        "cleaned_block": dirty_block.replace("就是就是就是", "就是"),
                        "source_header": "雨白（00:00:10 - 00:00:40）：",
                        "model": "gpt-4.1",
                        "updated_at": "now",
                    }
                },
            }
            cache_path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")

            plan = ch.build_plan(transcript_path, cache_path=cache_path)
            self.assertEqual(plan["header_text"], header)
            self.assertEqual(plan["stats"]["from_cache"], 1)
            self.assertEqual(plan["stats"]["pass_through"], 1)
            dirty = plan["blocks"][1]
            self.assertEqual(dirty["decision"], "from_cache")
            self.assertIsNotNone(dirty["cleaned_block"])

    def test_build_plan_marks_profile_hits_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_path = tmp / "01_transcript.md"
            transcript_path.write_text(
                (
                    "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n"
                    "## 正文\n\n"
                    "雨白（00:00:00 - 00:00:10）：\n执行小酒馆这期节目我很喜欢。\n"
                ),
                encoding="utf-8",
            )
            profile_path = tmp / "demo.profile.json"
            profile_path.write_text(
                json.dumps({"replacements": {"执行小酒馆": "知行小酒馆"}}, ensure_ascii=False),
                encoding="utf-8",
            )

            plan = ch.build_plan(transcript_path, profile_path=profile_path)
            block = plan["blocks"][0]
            self.assertEqual(block["decision"], "needs_model")
            self.assertIn("profile_replacement_hits", block["reasons"])
            self.assertIn("知行小酒馆", block["source_block"])

    def test_build_plan_marks_clean_block_pass_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_path = tmp / "01_transcript.md"
            transcript_path.write_text(TRANSCRIPT, encoding="utf-8")

            plan = ch.build_plan(transcript_path)

            self.assertEqual(plan["stats"]["needs_model"], 1)
            self.assertEqual(plan["stats"]["from_cache"], 0)
            self.assertEqual(plan["stats"]["pass_through"], 1)
            self.assertEqual([block["decision"] for block in plan["blocks"]], ["pass_through", "needs_model"])

    def test_assemble_writes_cleaned_output_and_updates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cache_path = tmp / ch.DEFAULT_CACHE_FILENAME
            plan_path = tmp / "cleanup_plan.json"
            output_path = tmp / "02_transcript_clean.md"

            plan = {
                "version": ch.PLAN_VERSION,
                "created_at": "now",
                "prompt_id": ch.PROMPT_ID,
                "transcript_path": str(tmp / "01_transcript.md"),
                "cache_path": str(cache_path),
                "profile_path": None,
                "header_text": "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n## 正文\n\n",
                "stats": {"total_blocks": 2, "needs_model": 2, "from_cache": 0},
                "blocks": [
                    {
                        "index": 1,
                        "cache_key": "aaa",
                        "decision": "needs_model",
                        "source_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "cleaned_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "header_line": "张潇雨（00:00:00 - 00:00:10）：",
                    },
                    {
                        "index": 2,
                        "cache_key": "bbb",
                        "decision": "needs_model",
                        "source_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是就是就是特别重要。",
                        "cleaned_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是特别重要。",
                        "header_line": "雨白（00:00:10 - 00:00:40）：",
                    },
                ],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

            ch.assemble_from_plan(plan_path, output_path, model="gpt-4.1")

            rendered = output_path.read_text(encoding="utf-8")
            self.assertIn("# 转写稿（忠实清洗版）", rendered)
            self.assertIn("- 清洗方式: 忠实语义清洗", rendered)
            self.assertIn("这个事情就是特别重要。", rendered)

            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("aaa", cache["entries"])
            self.assertIn("bbb", cache["entries"])
            self.assertEqual(cache["entries"]["bbb"]["model"], "gpt-4.1")

    def test_assemble_rejects_pass_through_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            plan_path = tmp / "cleanup_plan.json"
            output_path = tmp / "02_transcript_clean.md"
            plan = {
                "version": ch.PLAN_VERSION,
                "created_at": "now",
                "prompt_id": ch.PROMPT_ID,
                "transcript_path": str(tmp / "01_transcript.md"),
                "cache_path": str(tmp / ch.DEFAULT_CACHE_FILENAME),
                "profile_path": None,
                "header_text": "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n## 正文\n\n",
                "stats": {"total_blocks": 1, "needs_model": 0, "from_cache": 0},
                "blocks": [
                    {
                        "index": 1,
                        "cache_key": "aaa",
                        "decision": "pass_through",
                        "source_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "cleaned_block": None,
                        "header_line": "张潇雨（00:00:00 - 00:00:10）：",
                    }
                ],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

            ch.assemble_from_plan(plan_path, output_path, model="gpt-4.1")
            rendered = output_path.read_text(encoding="utf-8")
            self.assertIn("这是一段已经很干净的表达。", rendered)

    def test_assemble_fallback_source_renders_without_caching_unfinished_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cache_path = tmp / ch.DEFAULT_CACHE_FILENAME
            plan_path = tmp / "cleanup_plan.json"
            output_path = tmp / "02_transcript_clean.md"
            plan = {
                "version": ch.PLAN_VERSION,
                "created_at": "now",
                "prompt_id": ch.PROMPT_ID,
                "transcript_path": str(tmp / "01_transcript.md"),
                "cache_path": str(cache_path),
                "profile_path": None,
                "header_text": "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n## 正文\n\n",
                "stats": {"total_blocks": 2, "needs_model": 2, "from_cache": 0},
                "blocks": [
                    {
                        "index": 1,
                        "cache_key": "aaa",
                        "decision": "needs_model",
                        "source_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "cleaned_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "header_line": "张潇雨（00:00:00 - 00:00:10）：",
                    },
                    {
                        "index": 2,
                        "cache_key": "bbb",
                        "decision": "needs_model",
                        "source_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是就是就是特别重要。",
                        "cleaned_block": None,
                        "header_line": "雨白（00:00:10 - 00:00:40）：",
                    },
                ],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

            ch.assemble_from_plan(plan_path, output_path, model="gpt-4.1", fallback_source=True)

            rendered = output_path.read_text(encoding="utf-8")
            self.assertIn("这是一段已经很干净的表达。", rendered)
            self.assertIn("这个事情就是就是就是特别重要。", rendered)

            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("aaa", cache["entries"])
            self.assertNotIn("bbb", cache["entries"])

    def test_runner_resumes_and_assembles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_path = tmp / "01_transcript.md"
            plan_path = tmp / "cleanup_plan.json"
            output_path = tmp / "02_transcript_clean.md"
            transcript_path.write_text(TRANSCRIPT, encoding="utf-8")
            plan = {
                "version": ch.PLAN_VERSION,
                "created_at": "now",
                "prompt_id": ch.PROMPT_ID,
                "transcript_path": str(transcript_path),
                "cache_path": str(tmp / ch.DEFAULT_CACHE_FILENAME),
                "profile_path": None,
                "header_text": "# 转写稿\n\n- 生成时间: now\n- 来源链接: x\n- 集标题: 示例\n- 识别语言: zh\n\n## 正文\n\n",
                "stats": {"total_blocks": 2, "needs_model": 2, "from_cache": 0},
                "blocks": [
                    {
                        "index": 1,
                        "cache_key": "aaa",
                        "decision": "needs_model",
                        "source_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "cleaned_block": "张潇雨（00:00:00 - 00:00:10）：\n这是一段已经很干净的表达。",
                        "header_line": "张潇雨（00:00:00 - 00:00:10）：",
                    },
                    {
                        "index": 2,
                        "cache_key": "bbb",
                        "decision": "needs_model",
                        "source_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是就是就是特别重要。",
                        "cleaned_block": None,
                        "header_line": "雨白（00:00:10 - 00:00:40）：",
                    },
                ],
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

            def fake_run_codex_exec(prompt: str, *, model: str, output_path: Path) -> str:
                payload = {
                    "blocks": [
                        {
                            "index": 2,
                            "cleaned_block": "雨白（00:00:10 - 00:00:40）：\n这个事情就是特别重要。",
                        }
                    ]
                }
                output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return output_path.read_text(encoding="utf-8")

            with mock.patch.object(runner, "run_codex_exec", side_effect=fake_run_codex_exec):
                stats = runner.run_cleanup_plan(plan_path, output_path=output_path, model="gpt-5.4")

            updated = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(stats["processed"], 1)
            self.assertEqual(stats["batch_requests"], 1)
            self.assertEqual(updated["blocks"][1]["cleaned_block"], "雨白（00:00:10 - 00:00:40）：\n这个事情就是特别重要。")
            rendered = output_path.read_text(encoding="utf-8")
            self.assertIn("这个事情就是特别重要。", rendered)

    def test_runner_builds_batches_with_limits(self) -> None:
        blocks = [
            {"index": 1, "decision": "needs_model", "source_block": "a" * 200},
            {"index": 2, "decision": "needs_model", "source_block": "b" * 200},
            {"index": 3, "decision": "pass_through", "source_block": "clean"},
            {"index": 4, "decision": "needs_model", "source_block": "c" * 350},
            {"index": 5, "decision": "needs_model", "source_block": "d" * 350},
        ]

        batch1 = runner.next_batch(blocks, start_pos=0, max_blocks=3, max_chars=700)
        self.assertIsNotNone(batch1)
        assert batch1 is not None
        self.assertEqual(batch1.indexes, [1, 2])

        batch2 = runner.next_batch(blocks, start_pos=2, max_blocks=3, max_chars=700)
        self.assertIsNotNone(batch2)
        assert batch2 is not None
        self.assertEqual(batch2.indexes, [4, 5])

    def test_parse_batch_response_validates_headers_and_indexes(self) -> None:
        batch = runner.CleanupBatch(
            blocks=[
                {
                    "index": 12,
                    "header_line": "张潇雨（00:00:00 - 00:00:10）：",
                    "source_block": "张潇雨（00:00:00 - 00:00:10）：\n原文。",
                }
            ]
        )

        parsed = runner.parse_batch_response(
            json.dumps({"blocks": [{"index": 12, "cleaned_block": "张潇雨（00:00:00 - 00:00:10）：\n清洗后。"}]}, ensure_ascii=False),
            batch,
        )
        self.assertEqual(parsed, [(12, "张潇雨（00:00:00 - 00:00:10）：\n清洗后。")])

        with self.assertRaisesRegex(ValueError, "indexes do not match"):
            runner.parse_batch_response(
                json.dumps({"blocks": [{"index": 99, "cleaned_block": "张潇雨（00:00:00 - 00:00:10）：\n清洗后。"}]}, ensure_ascii=False),
                batch,
            )

    def test_build_plan_marks_sparse_punctuation_dirty(self) -> None:
        block = (
            "雨白（00:00:10 - 00:00:40）：\n"
            "这个事情特别重要如果你不这么看你就很难理解整个结构这里其实牵涉到很多层面的变化但是现在没有句号只有极少逗号"
            "我们还要继续补充很多内容让这一段变得足够长同时依然几乎没有任何像样的句末标点"
            "继续往下说的话你会发现这种文本读起来特别累而且机器虽然听懂了一点但是几乎没有帮你完成句子边界"
        )
        result = ch.analyze_block(block)
        self.assertEqual(result["decision"], "needs_model")
        self.assertIn("sparse_punctuation", result["reasons"])


if __name__ == "__main__":
    unittest.main()
