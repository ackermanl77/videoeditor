"""Core (Qt-free, FFmpeg-free) tests for the third improvement round.

B6/PG1 10-bit and HDR export, B7 stabilization and the colour/key
migration, F4 render queue model, F7 ripple move and roll-at-edge, F12
animatable effect parameters.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.commands import SplitClipCommand, UpdateClipPropertyCommand
from core.edit_ops import build_ripple_move_command, build_roll_at_edge
from core.effects import EffectInstance, effect_filters, legacy_effect_dicts
from core.export_presets import hdr_passthrough_choice
from core.exporter import ExportError, ExportSettings, Exporter
from core.filter_graph import build_filter_graph
from core.history import History
from core.media_asset import MediaAsset
from core.output_color import OutputColor, hdr_code_color, mastering_from_side_data, passthrough_choice
from core.project import PROJECT_FILE_VERSION, Project, ProjectSettings
from core.render_queue import JobStatus, RenderQueue
from core.stabilize import make_job, pending_jobs
from core.timeline_nav import clip_keyframe_points

SRC = os.path.join(tempfile.gettempdir(), "ve_p3_src.mp4")


def _video(start, length=4.0, path=SRC, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0.0, source_out=length,
                timeline_start=start, source_duration=20.0, **kw)


def _project(*clips, assets=()):
    project = Project()
    project.settings = ProjectSettings(width=64, height=36, fps=30.0)
    for asset in assets:
        project.add_media_asset(asset)
    project.tracks[0].clips += list(clips)
    return project


def _hdr_asset(transfer="smpte2084", **kw):
    return MediaAsset(path=SRC, duration=20.0, width=64, height=36, has_video=True, color_transfer=transfer,
                      color_primaries="bt2020", color_space="bt2020nc", bit_depth=10, **kw)


class LegacyColorFieldsTests(unittest.TestCase):  # B7 migration
    def test_constructor_fields_become_effects_in_the_old_render_order(self):
        clip = _video(0.0, brightness=0.2, chroma_key_enabled=True, effects=[EffectInstance.create("blur")])
        self.assertEqual([e.kind for e in clip.effects], ["colorkey", "color_correction", "blur"])
        self.assertEqual((clip.brightness, clip.chroma_key_enabled), (0.2, True))
        self.assertTrue(clip.has_color_correction and clip.has_chroma_key and clip.has_effects)

    def test_neutral_values_add_nothing_and_render_nothing(self):
        clip = _video(0.0, brightness=0.0, contrast=1.0, chroma_key_enabled=False)
        self.assertEqual(clip.effects, [])
        clip.gamma = 1.0
        self.assertEqual(clip.effects, [])
        self.assertNotIn("eq=", build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0)).filter_complex)

    def test_setting_a_key_colour_does_not_start_keying(self):
        clip = _video(0.0)
        clip.chroma_key_color = "#0000FF"
        self.assertFalse(clip.has_chroma_key)
        clip.chroma_key_enabled = True
        self.assertTrue(clip.has_chroma_key)
        self.assertIn("colorkey=0x0000FF:0.200:0.100", effect_filters(clip.effects, "post"))

    def test_property_command_undo_goes_through_the_effect(self):
        clip = _video(0.0)
        project = _project(clip)
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "contrast", 1.5))
        self.assertIn("contrast=1.500", effect_filters(clip.effects, "post"))
        history.undo()
        self.assertFalse(clip.has_color_correction)

    def test_version_2_file_migrates_and_renders_identically(self):
        old_clip = _video(0.0).to_dict()
        old_clip.update(brightness=0.2, contrast=1.5, saturation=0.5, gamma=1.1, chroma_key_enabled=True,
                        chroma_key_color="#00FF00", chroma_key_similarity=0.3, chroma_key_blend=0.15,
                        effects=[EffectInstance.create("sharpen").to_dict()])
        data = Project().to_dict()
        data["file_version"] = 2
        data["tracks"][0]["clips"] = [old_clip]
        project = Project.from_dict(json.loads(json.dumps(data)))
        clip = project.tracks[0].clips[0]
        self.assertEqual([e.kind for e in clip.effects], ["colorkey", "color_correction", "sharpen"])
        fc = build_filter_graph(project, ProjectSettings(64, 36, 30.0)).filter_complex
        # Exactly the text the pre-migration graph produced, in the same order.
        self.assertIn("colorkey=0x00FF00:0.300:0.150,eq=brightness=0.200:contrast=1.500:saturation=0.500"
                      ":gamma=1.100,unsharp=5:5:0.800", fc)
        saved = project.to_dict()
        self.assertEqual(saved["file_version"], PROJECT_FILE_VERSION)
        self.assertGreaterEqual(PROJECT_FILE_VERSION, 3)
        self.assertNotIn("brightness", saved["tracks"][0]["clips"][0])

    def test_disabled_but_configured_key_survives_migration_switched_off(self):
        effects = legacy_effect_dicts({"chroma_key_enabled": False, "chroma_key_color": "#0000FF"})
        self.assertEqual([(e["kind"], e["enabled"]) for e in effects], [("colorkey", False)])
        self.assertEqual(legacy_effect_dicts({"chroma_key_enabled": False, "brightness": 0.0}), [])


class AnimatedEffectParamTests(unittest.TestCase):  # F12
    def _animated(self):
        effect = EffectInstance.create("color_correction")
        effect.keyframes["brightness"] = [(0.0, -0.5), (2.0, 0.5)]
        return effect

    def test_animated_parameter_becomes_a_per_frame_expression(self):
        text = self._animated().filter()
        self.assertIn("eq=brightness='if(lt(t,0.000000)", text)
        self.assertTrue(text.endswith(":eval=frame"))
        self.assertIn("contrast=1.000", text)

    def test_single_keyframe_does_not_animate_and_non_animatable_params_ignore_keys(self):
        effect = EffectInstance.create("color_correction")
        effect.keyframes["brightness"] = [(1.0, 0.3)]
        self.assertFalse(effect.is_renderable())  # neutral plain values, nothing animated
        blur = EffectInstance.create("blur")
        blur.keyframes["sigma"] = [(0.0, 1.0), (1.0, 9.0)]
        self.assertEqual(blur.filter(), "gblur=sigma=4.000")

    def test_vignette_animation_is_quoted_and_evaluated_per_frame(self):
        effect = EffectInstance.create("vignette")
        effect.keyframes["angle"] = [(0.0, 10.0), (1.0, 60.0)]
        text = effect.filter()
        self.assertTrue(text.startswith("vignette=angle='PI*(if("), text)
        self.assertTrue(text.endswith(":eval=frame"))

    def test_keyframes_round_trip_and_values_are_clamped(self):
        effect = self._animated()
        effect.keyframes["gamma"] = [(0.0, 99.0), (1.0, 1.0)]
        again = EffectInstance.from_dict(json.loads(json.dumps(effect.to_dict())))
        self.assertEqual(again.keyframes["brightness"], [(0.0, -0.5), (2.0, 0.5)])
        self.assertEqual(again.animated_params()["gamma"][0][1], 3.0)

    def test_speed_ramp_segments_offset_the_clock(self):
        clip = _video(0.0, speed_keyframes=[(0.0, 1.0), (2.0, 2.0)], effects=[self._animated()])
        fc = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0)).filter_complex
        self.assertIn("lt(t,0.000000)", fc)  # first segment: plain clip-local t
        self.assertIn("lt((t+2.000000),0.000000)", fc)  # second segment starts 2 s into the clip

    def test_split_and_left_trim_rebase_effect_keyframes(self):
        clip = _video(0.0, effects=[self._animated()])
        project = _project(clip)
        History().execute(SplitClipCommand(project, clip.id, 1.0))
        first, second = sorted(project.tracks[0].clips, key=lambda c: c.timeline_start)
        self.assertEqual(first.effects[0].keyframes["brightness"], [(0.0, -0.5), (1.0, 0.0)])
        self.assertEqual(second.effects[0].keyframes["brightness"][0], (0.0, 0.0))
        trimmed = _video(0.0, effects=[self._animated()]).with_source_range(1.0, 4.0)
        self.assertEqual(trimmed.effects[0].keyframes["brightness"][0], (0.0, 0.0))

    def test_effect_keyframes_are_navigable(self):
        clip = _video(10.0, effects=[self._animated()])
        self.assertEqual(clip_keyframe_points(clip), [10.0, 12.0])


class StabilizationGraphTests(unittest.TestCase):  # B7 vidstab
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="ve_stab_")
        patcher = patch.dict(os.environ, {"VIDEO_EDITOR_STABILIZE_DIR": self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_transform_runs_on_source_frames_and_records_its_analysis(self):
        clip = _video(0.0, effects=[EffectInstance.create("stabilize", smoothing=20)])
        graph = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0))
        self.assertEqual(len(graph.stabilize_jobs), 1)
        job = graph.stabilize_jobs[0]
        self.assertEqual((job.start, job.length, job.shakiness, job.accuracy), (0.0, 4.0, 5, 15))
        fc = graph.filter_complex
        self.assertIn("trim=duration=4.000,setpts=PTS-STARTPTS,vidstabtransform=input=", fc)
        self.assertLess(fc.index("vidstabtransform"), fc.index("scale="))
        self.assertIn(":smoothing=20:optzoom=1", fc)

    def test_trimmed_export_decodes_a_stabilized_clip_from_its_start(self):
        clip = _video(0.0, length=10.0, effects=[EffectInstance.create("stabilize")])
        graph = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0), trim_range=(5.0, 8.0))
        self.assertEqual(graph.input_specs[0].pre_args[:2], ["-ss", "0.000"])
        self.assertIn("trim=start=5.000000", graph.filter_complex)
        plain = build_filter_graph(_project(_video(0.0, length=10.0)), ProjectSettings(64, 36, 30.0),
                                   trim_range=(5.0, 8.0))
        self.assertEqual(plain.input_specs[0].pre_args[:2], ["-ss", "5.000"])  # unstabilized clips still seek

    def test_frame_previews_skip_an_analysis_that_is_not_cached(self):
        clip = _video(0.0, effects=[EffectInstance.create("stabilize")])
        graph = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0), stabilize_cached_only=True)
        self.assertNotIn("vidstabtransform", graph.filter_complex)
        job = make_job(SRC, 0.0, 4.0, 5, 15)
        open(job.result_path, "w").close()
        graph = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0), stabilize_cached_only=True)
        self.assertIn("vidstabtransform", graph.filter_complex)

    def test_jobs_are_keyed_by_range_and_detection_settings(self):
        a = make_job(SRC, 0.0, 4.0, 5, 15)
        self.assertEqual(a.result_path, make_job(SRC, 0.0004, 4.0, 5, 15).result_path)
        self.assertNotEqual(a.result_path, make_job(SRC, 1.0, 4.0, 5, 15).result_path)
        self.assertNotEqual(a.result_path, make_job(SRC, 0.0, 4.0, 6, 15).result_path)
        self.assertEqual(len(pending_jobs([a, a])), 1)

    def test_speed_ramp_gets_one_analysis_per_segment(self):
        clip = _video(0.0, speed_keyframes=[(0.0, 1.0), (2.0, 2.0)], effects=[EffectInstance.create("stabilize")])
        graph = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0))
        self.assertEqual([(j.start, j.length) for j in graph.stabilize_jobs], [(0.0, 2.0), (2.0, 2.0)])

    def test_export_runs_missing_analyses_first_into_a_partial_file(self):
        project = _project(_video(0.0, effects=[EffectInstance.create("stabilize")]),
                           assets=[MediaAsset(path=SRC, duration=20.0, has_video=True)])
        open(SRC, "wb").close()
        calls = []

        class Proc:
            def __init__(self, args):
                self.returncode, self.stdout = 0, io.StringIO("")
                text = " ".join(args)
                if "vidstabdetect" in text:
                    # FFmpeg writes the result file named in the filter (".partial" until complete).
                    name = re.search(r"([0-9a-f]{20}\.trf\.partial)", text).group(1)
                    open(os.path.join(cache, name), "w").close()
                elif args[-1] != os.devnull:
                    open(args[-1], "wb").close()

            def wait(self):
                return 0

            def poll(self):
                return 0

        def fake_popen(args, **kw):
            calls.append(list(args))
            return Proc(args)

        out = os.path.join(tempfile.gettempdir(), "ve_p3_stab.mp4")
        cache = self.cache
        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch("core.exporter.popen", fake_popen):
            Exporter(project).export(ExportSettings(output_path=out, use_gpu=False))
            self.assertEqual(len(calls), 2)
            self.assertIn("vidstabdetect=shakiness=5:accuracy=15", " ".join(calls[0]))
            self.assertIn(".partial", " ".join(calls[0]))
            Exporter(project).export(ExportSettings(output_path=out, use_gpu=False))
        self.assertEqual(len(calls), 3)  # second export reuses the cached analysis
        self.assertEqual([f for f in os.listdir(self.cache) if f.endswith(".partial")], [])


class OutputColorTests(unittest.TestCase):  # B6 / PG1
    def test_modes(self):
        self.assertEqual(OutputColor.from_mode("sdr8"), OutputColor())
        self.assertEqual((OutputColor.from_mode("pq").transfer, OutputColor.from_mode("pq").matrix),
                         ("smpte2084", "bt2020nc"))
        self.assertTrue(OutputColor.from_mode("sdr10").is_10bit)
        self.assertEqual(OutputColor.from_mode("hlg").mode, "hlg")

    def test_ten_bit_graph_has_no_8bit_working_format(self):
        clip = _video(0.0, effects=[EffectInstance.create("lut3d", file="x.cube"), EffectInstance.create("blur")])
        fc = build_filter_graph(_project(clip), ProjectSettings(64, 36, 30.0),
                                output_color=OutputColor.from_mode("sdr10")).filter_complex
        self.assertNotRegex(fc, r"format=yuva?420p(?!10)")
        self.assertIn("overlay=format=yuv420p10:x=", fc)
        self.assertIn("setparams=color_primaries=bt709:color_trc=bt709", fc)

    def test_default_graph_is_unchanged_8bit(self):
        fc = build_filter_graph(_project(_video(0.0)), ProjectSettings(64, 36, 30.0)).filter_complex
        self.assertNotIn("p10", fc)
        self.assertIn("overlay=x=", fc)

    def test_hdr_passthrough_keeps_matching_sources_and_maps_sdr_to_reference_white(self):
        hdr = _hdr_asset()
        sdr = MediaAsset(path=SRC + "2", duration=20.0, width=64, height=36, has_video=True, color_space="smpte170m")
        project = _project(_video(0.0, asset_id=hdr.id), _video(4.0, path=sdr.path, asset_id=sdr.id), assets=[hdr, sdr])
        fc = build_filter_graph(project, ProjectSettings(64, 36, 30.0),
                                output_color=OutputColor.from_mode("pq")).filter_complex
        first, second = fc.split("[v0]")[0], fc.split("[v0]")[1].split("[v1]")[0]
        self.assertNotIn("zscale", first)  # PQ source in a PQ export: untouched
        self.assertNotIn("tonemap", fc)
        self.assertIn("zscale=tin=bt709:min=smpte170m", second)
        self.assertIn("t=smpte2084:m=bt2020nc:r=tv:npl=203", second)
        self.assertNotIn("out_color_matrix", second)  # zscale already converts the BT.601 matrix

    def test_sdr_export_still_tone_maps_hdr(self):
        hdr = _hdr_asset("arib-std-b67")
        fc = build_filter_graph(_project(_video(0.0, asset_id=hdr.id), assets=[hdr]),
                                ProjectSettings(64, 36, 30.0)).filter_complex
        self.assertIn("tonemap=tonemap=hable", fc)

    def test_hdr_text_colours_sit_at_reference_white(self):
        self.assertEqual(hdr_code_color("#FFFFFF", "hlg"), "#BFBFBF")  # 75 % HLG
        pq_white = int(hdr_code_color("#FFFFFF", "pq")[1:3], 16) / 255
        self.assertAlmostEqual(pq_white, 0.58, delta=0.01)
        self.assertEqual(hdr_code_color("#000000", "pq"), "#000000")
        self.assertEqual(OutputColor().text_color("#123456"), "#123456")

    def test_passthrough_offered_only_for_one_shared_transfer(self):
        self.assertEqual(passthrough_choice(["bt709", "smpte2084", "smpte2084"]), "pq")
        self.assertIsNone(passthrough_choice(["smpte2084", "arib-std-b67"]))
        self.assertIsNone(passthrough_choice(["bt709", ""]))
        hdr = _hdr_asset("arib-std-b67")
        self.assertEqual(hdr_passthrough_choice(_project(_video(0.0, asset_id=hdr.id), assets=[hdr])), "hlg")

    def test_mastering_metadata_in_x265_units(self):
        master, cll = mastering_from_side_data([
            {"side_data_type": "Mastering display metadata", "red_x": "34000/50000", "red_y": "16000/50000",
             "green_x": "13250/50000", "green_y": "34500/50000", "blue_x": "7500/50000", "blue_y": "3000/50000",
             "white_point_x": "15635/50000", "white_point_y": "16450/50000", "min_luminance": "1/10000",
             "max_luminance": "10000000/10000"},
            {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400},
        ])
        self.assertEqual(master, "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)")
        self.assertEqual(cll, "1000,400")
        self.assertEqual(mastering_from_side_data([]), ("", ""))


class TenBitExporterArgsTests(unittest.TestCase):  # B6 / PG1
    def _export(self, settings, project=None, gpu=False, ten_bit_gpu=True):
        project = project or _project(_video(0.0, length=5.0), assets=[MediaAsset(path=SRC, duration=5.0, has_video=True)])
        open(SRC, "wb").close()
        calls = []

        class Proc:
            def __init__(self, args):
                self.returncode, self.stdout = 0, io.StringIO("")
                if args[-1] != os.devnull:
                    open(args[-1], "wb").close()

            def wait(self):
                return 0

            def poll(self):
                return 0

        def fake_popen(args, **kw):
            calls.append(list(args))
            return Proc(args)

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch("core.exporter.popen", fake_popen), \
                patch("core.exporter.nvenc_available", return_value=gpu), \
                patch("core.exporter.nvenc_encoder_available", return_value=gpu), \
                patch("core.exporter.encoder_supports_10bit", return_value=ten_bit_gpu):
            Exporter(project).export(settings)
        return calls[-1]

    def _out(self, ext):
        return os.path.join(tempfile.gettempdir(), f"ve_p3_out{ext}")

    def test_ten_bit_pixel_formats_per_codec(self):
        for ext, codec, gpu, expected in (
            (".mkv", "hevc", False, ["-pix_fmt", "yuv420p10le", "-profile:v", "main10"]),
            (".mkv", "hevc", True, ["-pix_fmt", "p010le", "-profile:v", "main10"]),
            (".mkv", "av1", True, ["-pix_fmt", "p010le"]),
            (".mkv", "av1", False, ["-pix_fmt", "yuv420p10le"]),
            (".mov", "dnxhr", False, ["-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"]),
            (".mov", "prores", False, ["-pix_fmt", "yuv422p10le"]),
        ):
            with self.subTest(codec=codec, gpu=gpu):
                args = self._export(ExportSettings(self._out(ext), video_codec=codec, use_gpu=gpu, color_mode="sdr10"),
                                    gpu=gpu)
                joined = " ".join(args)
                self.assertIn(" ".join(expected), joined)
                self.assertIn("format=yuva420p10le", joined)

    def test_eight_bit_dnxhr_stays_hq(self):
        args = self._export(ExportSettings(self._out(".mov"), video_codec="dnxhr", use_gpu=False))
        self.assertIn("dnxhr_hq", args)

    def test_h264_cannot_be_ten_bit(self):
        with self.assertRaises(ExportError):
            self._export(ExportSettings(self._out(".mp4"), video_codec="h264", use_gpu=False, color_mode="sdr10"))

    def test_gpu_without_ten_bit_support_falls_back_to_the_cpu_encoder(self):
        args = self._export(ExportSettings(self._out(".mkv"), video_codec="hevc", use_gpu=True, color_mode="sdr10"),
                            gpu=True, ten_bit_gpu=False)
        self.assertIn("libx265", args)

    def test_hdr10_tags_and_mastering_metadata(self):
        hdr = _hdr_asset(mastering_display="G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", max_cll="1000,400")
        project = _project(_video(0.0, asset_id=hdr.id), assets=[hdr])
        args = self._export(ExportSettings(self._out(".mkv"), video_codec="hevc", use_gpu=False, color_mode="pq"),
                            project=project)
        joined = " ".join(args)
        self.assertIn("-colorspace bt2020nc -color_primaries bt2020 -color_trc smpte2084", joined)
        params = args[args.index("-x265-params") + 1]
        for part in ("hdr10=1", "master-display=G(1,2)B(3,4)R(5,6)WP(7,8)L(9,1)", "max-cll=1000,400",
                     "transfer=smpte2084", "repeat-headers=1"):
            self.assertIn(part, params)
        self.assertIn("setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc", joined)

    def test_hlg_has_no_hdr10_metadata(self):
        hdr = _hdr_asset("arib-std-b67")
        args = self._export(ExportSettings(self._out(".mkv"), video_codec="hevc", use_gpu=False, color_mode="hlg"),
                            project=_project(_video(0.0, asset_id=hdr.id), assets=[hdr]))
        params = args[args.index("-x265-params") + 1]
        self.assertIn("transfer=arib-std-b67", params)
        self.assertNotIn("hdr10", params)

    def test_gif_ignores_the_colour_mode(self):
        args = self._export(ExportSettings(self._out(".gif"), color_mode="pq", use_gpu=False))
        self.assertNotIn("p10", " ".join(args))


class RenderQueueModelTests(unittest.TestCase):  # F4
    def setUp(self):
        self.queue = RenderQueue()
        self.project = _project(_video(0.0))

    def _add(self, name):
        return self.queue.add(self.project, ExportSettings(output_path=os.path.join("out", name)))

    def test_jobs_are_snapshots(self):
        job = self._add("a.mp4")
        self.project.tracks[0].clips[0].timeline_start = 9.0
        self.assertEqual(job.project.tracks[0].clips[0].timeline_start, 0.0)
        self.assertEqual(job.name, "a.mp4")

    def test_order_cancel_remove_retry(self):
        a, b, c = self._add("a.mp4"), self._add("b.mp4"), self._add("c.mp4")
        self.assertTrue(self.queue.move(c.id, -1))
        self.assertEqual([j.name for j in self.queue.jobs], ["a.mp4", "c.mp4", "b.mp4"])
        self.queue.mark_started(a)
        self.assertFalse(self.queue.remove(a.id))  # running
        self.assertFalse(self.queue.move(a.id, 1))
        self.assertTrue(self.queue.cancel_queued(c.id))
        self.assertIs(self.queue.next_queued(), b)
        self.queue.mark_progress(a, 0.5, "Rendering... 50% (2.0x realtime, ~01:12 left)")
        self.assertEqual(a.eta, "01:12")
        self.queue.mark_failed(a, "boom")
        self.assertEqual(a.status, JobStatus.FAILED)
        self.assertTrue(self.queue.requeue(a.id))
        self.assertEqual(self.queue.pending_count(), 2)
        self.queue.mark_started(b)
        self.queue.mark_done(b, ["warn"])
        self.assertEqual(self.queue.clear_finished(), 2)  # b (done) and c (cancelled)
        self.assertEqual([j.id for j in self.queue.jobs], [a.id])


class RippleMoveAndRollEdgeTests(unittest.TestCase):  # F7
    def setUp(self):
        self.a, self.b, self.c = _video(0.0), _video(4.0), _video(10.0)
        self.project = _project(self.a, self.b, self.c)
        self.history = History(validator=self.project.validate)

    def test_ripple_move_shifts_the_clip_and_everything_after_it(self):
        self.history.execute(build_ripple_move_command(self.project, self.b.id, 1.5))
        self.assertEqual([c.timeline_start for c in (self.a, self.b, self.c)], [0.0, 5.5, 11.5])
        self.history.undo()
        self.assertEqual(self.b.timeline_start, 4.0)

    def test_ripple_move_is_refused_into_an_earlier_clip(self):
        self.assertIsNone(build_ripple_move_command(self.project, self.b.id, -1.0))
        self.assertIsNone(build_ripple_move_command(self.project, self.a.id, -0.5))

    def test_ripple_move_brings_linked_clips(self):
        audio = Clip(media_type=MediaType.AUDIO, source_path=SRC, source_out=4.0, timeline_start=10.0,
                     link_group="g", source_duration=20.0)
        self.c.link_group = "g"
        self.project.tracks[1].clips.append(audio)
        self.history.execute(build_ripple_move_command(self.project, self.b.id, 1.0))
        self.assertEqual(audio.timeline_start, 11.0)

    def test_roll_at_left_edge_rolls_with_the_previous_clip(self):
        self.b.source_in, self.b.source_out = 2.0, 6.0  # media before its in point to roll into
        self.history.execute(build_roll_at_edge(self.project, self.b.id, "left", 3.0))
        self.assertEqual((self.a.timeline_end, self.b.timeline_start, self.b.source_in), (3.0, 3.0, 1.0))
        self.assertIsNone(build_roll_at_edge(self.project, self.c.id, "left", 9.5))  # gap: no neighbour
        self.history.execute(build_roll_at_edge(self.project, self.a.id, "right", 3.5))
        self.assertEqual(self.b.timeline_start, 3.5)


if __name__ == "__main__":
    unittest.main()
