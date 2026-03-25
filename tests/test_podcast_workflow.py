import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "podcast_workflow.py"
SPEC = importlib.util.spec_from_file_location("podcast_workflow", MODULE_PATH)
pw = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = pw
SPEC.loader.exec_module(pw)


class PodcastWorkflowUnitTests(unittest.TestCase):
    def test_classify_source_show_vs_episode(self) -> None:
        show_info = {"_type": "playlist", "entries": [{"title": "A"}]}
        episode_info = {"title": "single"}

        self.assertEqual(pw.classify_source(show_info), "show")
        self.assertEqual(pw.classify_source(episode_info), "episode")

    def test_choose_episode_by_index(self) -> None:
        eps = [
            pw.EpisodeCandidate("ep1", "https://x/1", 1, 100, None, None),
            pw.EpisodeCandidate("ep2", "https://x/2", 2, 90, None, None),
            pw.EpisodeCandidate("ep3", "https://x/3", 3, 80, None, None),
        ]
        selected = pw.choose_episode(eps, 2)
        self.assertEqual(selected.title, "ep2")

    def test_choose_episode_interactive(self) -> None:
        eps = [
            pw.EpisodeCandidate("ep1", "https://x/1", 1, 100, None, None),
            pw.EpisodeCandidate("ep2", "https://x/2", 2, 90, None, None),
        ]
        with mock.patch.object(pw.sys.stdin, "isatty", return_value=True):
            selected = pw.choose_episode(eps, None, input_fn=lambda _: "1")
        self.assertEqual(selected.title, "ep1")

    def test_profile_auto_match_and_apply_to_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "demo.profile.json"
            profile_path.write_text(
                (
                    "{\n"
                    '  "name": "demo",\n'
                    '  "match": {"title_regex": "示例播客"},\n'
                    '  "speaker_a_name": "雨白",\n'
                    '  "speaker_b_name": "张潇雨",\n'
                    '  "speaker_name_map": {"SPEAKER_00": "雨白", "SPEAKER_01": "张潇雨"},\n'
                    '  "noise_phrases": ["广告词"],\n'
                    '  "replacements": {"播客": "播客节目"}\n'
                    "}\n"
                ),
                encoding="utf-8",
            )

            selected = pw.EpisodeCandidate("示例播客 第1期", "https://example.com/ep", None, 0, None, None)
            profile = pw.resolve_profile(
                explicit_profile=None,
                profile_dir=tmp_dir,
                input_url="https://example.com/show",
                selected_episode=selected,
            )

            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile.speaker_name_map["SPEAKER_00"], "雨白")

            segments = [
                pw.Segment(t0_ms=0, t1_ms=1, text="这是广告词"),
                pw.Segment(t0_ms=1, t1_ms=2, text="这是一段播客"),
            ]
            applied = pw.apply_profile_to_segments(segments, profile)
            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0].text, "这是一段播客节目")

    def test_build_speaker_name_map_honors_priority(self) -> None:
        profile = pw.PodcastProfile(
            name="demo",
            path=Path("/tmp/demo.profile.json"),
            source_url_regex=None,
            title_regex=None,
            input_url_regex=None,
            speaker_a_name="主持人A",
            speaker_b_name="主持人B",
            speaker_name_map={"SPEAKER_00": "资料里的A", "SPEAKER_02": "资料里的C"},
            noise_phrases=[],
            replacements={},
        )
        args = Namespace(
            speaker_a_name="CLI-A",
            speaker_b_name=None,
            speaker_name_map=["SPEAKER_02=CLI-C", "SPEAKER_03=CLI-D"],
        )

        mapping = pw.build_speaker_name_map(args, profile)

        self.assertEqual(mapping["SPEAKER_00"], "CLI-A")
        self.assertEqual(mapping["SPEAKER_01"], "主持人B")
        self.assertEqual(mapping["SPEAKER_02"], "CLI-C")
        self.assertEqual(mapping["SPEAKER_03"], "CLI-D")

    def test_transcript_markdown_merges_speaker_turns(self) -> None:
        segments = [
            pw.Segment(t0_ms=0, t1_ms=1000, text="你好", speaker="SPEAKER_00"),
            pw.Segment(t0_ms=1000, t1_ms=2000, text="世界", speaker="SPEAKER_00"),
            pw.Segment(t0_ms=2000, t1_ms=3000, text="收到", speaker="SPEAKER_01"),
        ]

        rendered = pw.transcript_markdown(
            segments,
            source_url="https://example.com/ep",
            episode_title="示例",
            language="zh",
            speaker_names={"SPEAKER_00": "张潇雨", "SPEAKER_01": "雨白"},
        )

        self.assertIn("张潇雨（00:00:00 - 00:00:02）：\n你好，世界。", rendered)
        self.assertIn("\n\n雨白（00:00:02 - 00:00:03）：\n收到。", rendered)
        self.assertNotIn("SPEAKER_00", rendered)

    def test_merge_segments_into_turns_splits_long_same_speaker_runs(self) -> None:
        segments = [
            pw.Segment(t0_ms=0, t1_ms=1000, text="第一段", speaker="SPEAKER_00"),
            pw.Segment(t0_ms=1000, t1_ms=2000, text="第二段", speaker="SPEAKER_00"),
            pw.Segment(t0_ms=2000, t1_ms=3000, text="第三段", speaker="SPEAKER_00"),
            pw.Segment(t0_ms=3000, t1_ms=4000, text="第四段", speaker="SPEAKER_00"),
        ]

        turns = pw.merge_segments_into_turns(segments)

        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].parts, ["第一段", "第二段", "第三段"])
        self.assertEqual(turns[1].parts, ["第四段"])

    def test_lightly_punctuate_fragment_adds_commas_for_common_discourse_markers(self) -> None:
        rendered = pw.lightly_punctuate_fragment("开玩笑然后其实这事没错对吧我们再说")
        self.assertEqual(rendered, "开玩笑，然后，其实这事没错，对吧，我们再说")

    def test_transcript_markdown_wraps_long_turn_body(self) -> None:
        text = (
            "这是第一句，主要是为了测试长段落自动换行。"
            "这是第二句，也会继续补充一些内容，让这一段超过一百个字。"
            "最后再来一句，确保会在合适的标点位置断开，读起来不会太累。"
            "再补一段内容，模拟真实播客里单人连续说很多话的情况，避免输出变成一整块难读的文字。"
        )
        segments = [pw.Segment(t0_ms=0, t1_ms=1000, text=text, speaker="SPEAKER_00")]

        rendered = pw.transcript_markdown(
            segments,
            source_url="https://example.com/ep",
            episode_title="示例",
            language="zh",
            speaker_names={"SPEAKER_00": "张潇雨"},
        )

        body = rendered.split("张潇雨（00:00:00 - 00:00:01）：\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertGreater(len(body), 1)
        for line in body:
            self.assertLessEqual(len(line), pw.TURN_WRAP_CHARS)

    def test_preflight_requires_hf_token(self) -> None:
        args = Namespace(hf_token=None, diarize_model=pw.DEFAULT_DIARIZE_MODEL, skip_diarization=False)
        with (
            mock.patch.object(pw, "resolve_executable", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch.dict(pw.os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(pw.WorkflowError, "hf-token"):
                pw.preflight(args)

    def test_preflight_allows_missing_hf_token_when_skipping_diarization(self) -> None:
        args = Namespace(hf_token=None, diarize_model=pw.DEFAULT_DIARIZE_MODEL, skip_diarization=True)
        with (
            mock.patch.object(pw, "resolve_executable", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch.object(pw, "verify_pyannote_access") as verify_mock,
            mock.patch.dict(pw.os.environ, {}, clear=True),
        ):
            yt_dlp_bin, ffmpeg_bin, token = pw.preflight(args)

        self.assertEqual(yt_dlp_bin, "/usr/bin/yt-dlp")
        self.assertEqual(ffmpeg_bin, "/usr/bin/ffmpeg")
        self.assertIsNone(token)
        verify_mock.assert_not_called()

    def test_should_keep_awake_only_on_macos_and_first_run(self) -> None:
        args = Namespace(keep_awake=True)
        with (
            mock.patch.object(pw, "sys") as sys_mock,
            mock.patch.dict(pw.os.environ, {}, clear=True),
        ):
            sys_mock.platform = "darwin"
            self.assertTrue(pw.should_keep_awake(args))

        with (
            mock.patch.object(pw, "sys") as sys_mock,
            mock.patch.dict(pw.os.environ, {pw.CAFFEINATE_ENV_FLAG: "1"}, clear=True),
        ):
            sys_mock.platform = "darwin"
            self.assertFalse(pw.should_keep_awake(args))

    def test_rerun_with_caffeinate_wraps_current_process(self) -> None:
        with (
            mock.patch.object(pw, "resolve_executable", return_value="/usr/bin/caffeinate"),
            mock.patch.object(pw.subprocess, "run") as run_mock,
            mock.patch.object(pw, "log"),
            mock.patch.object(pw.sys, "executable", "/tmp/.venv/bin/python"),
            mock.patch.object(pw.sys, "argv", ["scripts/podcast_workflow.py", "--url", "https://example.com"]),
        ):
            run_mock.return_value = mock.Mock(returncode=0)
            rc = pw.rerun_with_caffeinate()

        self.assertEqual(rc, 0)
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:3], ["/usr/bin/caffeinate", "-dimsu", "/tmp/.venv/bin/python"])
        self.assertEqual(cmd[3:], ["scripts/podcast_workflow.py", "--url", "https://example.com"])
        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env[pw.CAFFEINATE_ENV_FLAG], "1")


class PodcastWorkflowExecutionTests(unittest.TestCase):
    def _make_args(self, out_root: str, episode_index: int | None = None) -> Namespace:
        return Namespace(
            url="https://example.com/podcast",
            episode_index=episode_index,
            out_root=out_root,
            profile=None,
            profile_dir="./scripts/podcast_profiles",
            speaker_a_name=None,
            speaker_b_name=None,
            speaker_name_map=[],
            model=pw.DEFAULT_MODEL,
            language="zh",
            batch_size=8,
            chunk_size=30,
            device="cpu",
            device_index=0,
            compute_type="int8",
            model_dir=None,
            model_cache_only=False,
            align_model=None,
            vad_method="pyannote",
            min_speakers=None,
            max_speakers=None,
            hf_token="hf_xxx",
            diarize_model=pw.DEFAULT_DIARIZE_MODEL,
            skip_diarization=False,
            keep_awake=False,
            verbose=True,
            print_progress=False,
            retries=1,
        )

    def test_mocked_integration_generates_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as out_root, tempfile.TemporaryDirectory() as fake_tmp:
            args = self._make_args(out_root, episode_index=1)
            src = Path(fake_tmp) / "source.webm"
            src.write_bytes(b"source")

            def fake_transcode(_ffmpeg, _src, dst, retries=None):
                Path(dst).write_bytes(b"mp3")

            whisperx_result = pw.WhisperXRunResult(
                transcript_result={
                    "language": "zh",
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "你好，世界", "speaker": "SPEAKER_00"},
                    ],
                },
                diarization_records=[
                    {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "label": "A"},
                ],
                language="zh",
            )

            show_info = {
                "_type": "playlist",
                "entries": [
                    {
                        "title": "第1期",
                        "webpage_url": "https://example.com/ep1",
                        "playlist_index": 1,
                        "release_timestamp": 200,
                    }
                ],
            }

            with (
                mock.patch.object(pw, "preflight", return_value=("yt-dlp", "ffmpeg", "hf_xxx")),
                mock.patch.object(pw, "inspect_source", return_value=show_info),
                mock.patch.object(pw, "download_audio", return_value=src),
                mock.patch.object(pw, "transcode_to_mp3", side_effect=fake_transcode),
                mock.patch.object(pw, "transcribe_audio", return_value=whisperx_result),
            ):
                out_dir = pw.execute_workflow(args)

            self.assertTrue((out_dir / "audio.mp3").exists())
            self.assertTrue((out_dir / "01_transcript.md").exists())
            self.assertTrue((out_dir / "01_transcript.json").exists())
            self.assertTrue((out_dir / "01_diarization.json").exists())
            self.assertFalse((out_dir / "02_transcript_clean.md").exists())

            transcript = (out_dir / "01_transcript.md").read_text(encoding="utf-8")
            self.assertIn("SPEAKER_00（00:00:00 - 00:00:01）：", transcript)
            self.assertIn("你好，世界。", transcript)

            diarization = json.loads((out_dir / "01_diarization.json").read_text(encoding="utf-8"))
            self.assertEqual(diarization["records"][0]["speaker"], "SPEAKER_00")

    def test_mocked_integration_skip_diarization_still_generates_transcript_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as out_root, tempfile.TemporaryDirectory() as fake_tmp:
            args = self._make_args(out_root, episode_index=1)
            args.hf_token = None
            args.skip_diarization = True
            src = Path(fake_tmp) / "source.webm"
            src.write_bytes(b"source")

            def fake_transcode(_ffmpeg, _src, dst, retries=None):
                Path(dst).write_bytes(b"mp3")

            whisperx_result = pw.WhisperXRunResult(
                transcript_result={
                    "language": "zh",
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "你好，世界"},
                    ],
                },
                diarization_records=[],
                language="zh",
            )

            episode_info = {
                "title": "第1期",
                "webpage_url": "https://example.com/ep1",
                "duration": 60,
            }

            with (
                mock.patch.object(pw, "preflight", return_value=("yt-dlp", "ffmpeg", None)),
                mock.patch.object(pw, "inspect_source", return_value=episode_info),
                mock.patch.object(pw, "download_audio", return_value=src),
                mock.patch.object(pw, "transcode_to_mp3", side_effect=fake_transcode),
                mock.patch.object(pw, "transcribe_audio", return_value=whisperx_result),
            ):
                out_dir = pw.execute_workflow(args)

            self.assertTrue((out_dir / "audio.mp3").exists())
            self.assertTrue((out_dir / "01_transcript.md").exists())
            self.assertTrue((out_dir / "01_transcript.json").exists())
            self.assertTrue((out_dir / "01_diarization.json").exists())

            transcript = (out_dir / "01_transcript.md").read_text(encoding="utf-8")
            self.assertIn("（00:00:00 - 00:00:01）：\n你好，世界。", transcript)

            diarization = json.loads((out_dir / "01_diarization.json").read_text(encoding="utf-8"))
            self.assertTrue(diarization["skipped"])
            self.assertEqual(diarization["records"], [])


if __name__ == "__main__":
    unittest.main()
