"""Core (Qt-free, FFmpeg-free) tests for the P2 batch of the improvement report.

B5 audio crossfades/volume automation, B6 colour handling, B7 effect stack,
B8 versioned loading, B9 relative paths/relink, F4 presets/NTSC/ETA,
F6/F7 multi-clip edits, F12 keyframe points, PG1 codecs, R1 left-trim
re-basing, R2 per-session autosave, R4 clean index, R5 timeouts, R6 validation.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import __version__
from core.autosave import AutosaveSession, find_orphaned_autosaves, pid_alive
from core.clip import Clip, MediaType, ReframeMode, TransitionType
from core.commands import AddClipCommand, MoveClipCommand, TrimClipCommand, UpdateClipPropertyCommand
from core.edit_ops import (
    build_delete_clips_command,
    build_detach_audio_command,
    build_duplicate_command,
    build_insert_command,
    build_move_clips_command,
    build_overwrite_command,
    build_paste_attributes_command,
    build_paste_command,
    build_roll_command,
    build_slide_command,
    build_slip_command,
    copy_clips,
    linked_clip_ids,
)
from core.effects import REGISTRY, EffectInstance, effect_filters
from core.export_presets import (
    dominant_source,
    load_user_presets,
    nearest_fps_choice,
    save_user_preset,
    source_defaults,
)
from core.exporter import (
    BT709_OUTPUT_TAGS,
    ExportError,
    ExportSettings,
    Exporter,
    NVENC_TUNING,
    progress_message,
    resolve_codecs,
)
from core.filter_graph import build_filter_graph, fps_expr
from core.history import History, InvalidEditError
from core.media_asset import MediaAsset
from core.project import PROJECT_FILE_VERSION, Project, ProjectSettings, ProjectVersionError
from core.relink import RelinkMediaCommand, build_relink_command, find_candidates, matches, relink_from_folder
from core.timeline_nav import clip_keyframe_points
from core.track import TrackType

SRC = os.path.join(tempfile.gettempdir(), "ve_p2_src.mp4")


def _video(start, length=4.0, path=SRC, **kw):
    return Clip(media_type=MediaType.VIDEO, source_path=path, source_in=0.0, source_out=length,
                timeline_start=start, source_duration=20.0, **kw)


def _audio(start, length=4.0, path="/m/music.wav", **kw):
    return Clip(media_type=MediaType.AUDIO, source_path=path, source_in=0.0, source_out=length,
                timeline_start=start, source_duration=20.0, **kw)


def _strict_history(project):
    return History(validator=project.validate)


# ---------------------------------------------------------------------------
class ProjectVersioningTests(unittest.TestCase):  # B8
    def test_newer_file_version_is_refused_with_clear_message(self):
        data = Project().to_dict()
        data["file_version"] = PROJECT_FILE_VERSION + 97
        data["app_version"] = "9.9.9"
        with self.assertRaises(ProjectVersionError) as ctx:
            Project.from_dict(data)
        self.assertIn("9.9.9", str(ctx.exception))
        self.assertIn("newer", str(ctx.exception).lower() + "newer")

    def test_unknown_keys_and_enum_values_are_tolerated(self):
        project = Project()
        project.add_media_asset(MediaAsset(path="/m/a.mp4"))
        project.tracks[0].clips.append(_video(0.0))
        data = project.to_dict()
        data["settings"]["color_space"] = "rec2020"
        data["media_pool"][0]["future_field"] = 1
        data["tracks"][0]["clips"][0]["transition_out"] = "zoom"
        data["tracks"][0]["clips"][0]["reframe_mode"] = "smart"
        data["tracks"][0]["clips"].append(dict(data["tracks"][0]["clips"][0], id="x", media_type="hologram"))
        loaded = Project.from_dict(data)
        clip = loaded.tracks[0].clips[0]
        self.assertEqual(clip.transition_out, TransitionType.NONE)
        self.assertEqual(clip.reframe_mode, ReframeMode.FIT)
        self.assertEqual(len(loaded.tracks[0].clips), 1)  # the unreadable clip is skipped...
        self.assertEqual(len(loaded.load_warnings), 3)  # ...and every substitution is reported

    def test_saved_file_carries_format_and_app_version(self):
        data = Project().to_dict()
        self.assertEqual(data["file_version"], PROJECT_FILE_VERSION)
        self.assertEqual(data["app_version"], __version__)

    def test_version_1_file_is_migrated_and_clips_linked_to_assets(self):
        data = {
            "file_version": 1,
            "name": "old",
            "media_pool": [{"id": "asset1", "path": "/m/a.mp4", "duration": 4.0}],
            "tracks": [{"track_type": "video", "id": "t1", "clips": [
                {"media_type": "video", "source_path": "/m/a.mp4", "source_out": 4.0,
                 "track_index": 0, "transition_in": "none"}]}],
        }
        project = Project.from_dict(data)
        self.assertEqual(project.tracks[0].clips[0].asset_id, "asset1")
        self.assertEqual(project.load_warnings, [])


class RelativePathAndRelinkTests(unittest.TestCase):  # B9
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ve_relink_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x")
        return path

    def test_moved_project_folder_finds_media_through_relative_paths(self):
        media = self._touch("proj", "media", "a.mp4")
        project = Project()
        asset = MediaAsset(path=media, duration=4.0, has_video=True)
        project.add_media_asset(asset)
        project.tracks[0].clips.append(_video(0.0, path=media, asset_id=asset.id))
        project.save(os.path.join(self.tmp, "proj", "p.vproj"))
        shutil.move(os.path.join(self.tmp, "proj"), os.path.join(self.tmp, "moved"))

        loaded = Project.load(os.path.join(self.tmp, "moved", "p.vproj"))
        expected = os.path.join(self.tmp, "moved", "media", "a.mp4")
        self.assertEqual(os.path.normcase(loaded.media_pool[0].path), os.path.normcase(expected))
        self.assertEqual(os.path.normcase(loaded.tracks[0].clips[0].source_path), os.path.normcase(expected))

    def test_relink_updates_asset_and_every_clip_and_undoes(self):
        project = Project()
        asset = MediaAsset(path="/old/a.mp4", duration=4.0, has_video=True)
        project.add_media_asset(asset)
        by_id = _video(0.0, path="/old/a.mp4", asset_id=asset.id)
        by_path = _video(5.0, path="/old/a.mp4")  # an older project's clip with no asset id
        project.tracks[0].clips += [by_id, by_path]
        history = History()
        history.execute(RelinkMediaCommand(project, asset.id, "/new/a.mp4"))
        self.assertEqual(asset.path, "/new/a.mp4")
        self.assertEqual({by_id.source_path, by_path.source_path}, {"/new/a.mp4"})
        history.undo()
        self.assertEqual({asset.path, by_id.source_path, by_path.source_path}, {"/old/a.mp4"})

    def test_folder_search_matches_by_name_and_verifies_by_probe(self):
        good = self._touch("card", "sub", "C0001.MP4")
        self._touch("other", "c0001.mp4")
        project = Project()
        asset = MediaAsset(path="/gone/C0001.MP4", duration=10.0, width=1920, height=1080, has_video=True)
        project.add_media_asset(asset)
        self.assertEqual(len(find_candidates(asset, self.tmp)), 2)

        def probe(path):
            ok = os.path.normcase(path) == os.path.normcase(good)
            return MediaAsset(path=path, duration=10.03 if ok else 3.0, width=1920, height=1080, has_video=True)

        mapping = relink_from_folder(project, self.tmp, probe=probe)
        self.assertEqual(mapping, {asset.id: good})
        self.assertIsNotNone(build_relink_command(project, mapping))

    def test_matches_rejects_wrong_duration_or_size(self):
        asset = MediaAsset(path="a", duration=10.0, width=1920, height=1080, has_video=True)
        self.assertFalse(matches(asset, MediaAsset(path="b", duration=10.5, width=1920, height=1080, has_video=True)))
        self.assertFalse(matches(asset, MediaAsset(path="b", duration=10.0, width=1280, height=720, has_video=True)))


class ValidationAndCleanIndexTests(unittest.TestCase):  # R6, R4
    def test_validate_reports_overlaps(self):
        project = Project()
        project.tracks[0].clips += [_video(0.0), _video(2.0)]
        problems = project.validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("overlap", problems[0])

    def test_strict_history_rejects_and_undoes_an_overlapping_edit(self):
        project = Project()
        a, b = _video(0.0), _video(4.0)
        project.tracks[0].clips += [a, b]
        history = _strict_history(project)
        with self.assertRaises(InvalidEditError):
            history.execute(MoveClipCommand(project, b.id, 2.0, project.tracks[0].id))
        self.assertEqual(b.timeline_start, 4.0)
        self.assertFalse(history.can_undo)

    def test_existing_problems_do_not_block_unrelated_edits(self):
        project = Project()
        project.tracks[0].clips += [_video(0.0), _video(2.0)]
        third = _video(20.0)
        project.tracks[0].clips.append(third)
        history = _strict_history(project)
        history.execute(UpdateClipPropertyCommand(project, third.id, "volume", 0.5))
        self.assertEqual(third.volume, 0.5)

    def test_clean_index_follows_undo_and_redo(self):
        project = Project()
        clip = _video(0.0)
        project.tracks[0].clips.append(clip)
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.5))
        history.mark_clean()
        self.assertTrue(history.is_clean)
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.7))
        self.assertFalse(history.is_clean)
        history.undo()
        self.assertTrue(history.is_clean)
        history.undo()
        self.assertFalse(history.is_clean)
        history.redo()
        self.assertTrue(history.is_clean)

    def test_saved_state_in_a_discarded_redo_branch_is_never_clean_again(self):
        project = Project()
        clip = _video(0.0)
        project.tracks[0].clips.append(clip)
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.5))
        history.mark_clean()
        history.undo()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.9))
        history.undo()
        self.assertFalse(history.is_clean)

    def test_mergeable_edit_after_save_is_a_new_step(self):
        project = Project()
        clip = _video(0.0)
        project.tracks[0].clips.append(clip)
        history = History()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.5, mergeable=True))
        history.mark_clean()
        history.execute(UpdateClipPropertyCommand(project, clip.id, "volume", 0.6, mergeable=True))
        self.assertFalse(history.is_clean)
        history.undo()
        self.assertTrue(history.is_clean)
        self.assertEqual(clip.volume, 0.5)


class AudioCrossfadeAndAutomationTests(unittest.TestCase):  # B5
    def _project(self):
        project = Project()
        audio_track = project.tracks[1]
        a = _audio(0.0, transition_out=TransitionType.CROSSFADE, transition_duration=1.0)
        b = _audio(4.0, path="/m/b.wav")
        audio_track.clips += [a, b]
        return project, a, b

    def test_adjacent_audio_clips_crossfade_with_equal_power_curves(self):
        project, _a, _b = self._project()
        fc = build_filter_graph(project, ProjectSettings()).filter_complex
        self.assertIn("afade=t=out:st=3.000:d=1.000:curve=qsin", fc)
        self.assertIn("afade=t=in:st=0:d=1.000:curve=qsin", fc)
        self.assertIn("adelay=delays=3000:all=1", fc)  # B starts d seconds early, overlapping A

    def test_gap_means_no_crossfade(self):
        project, _a, b = self._project()
        b.timeline_start = 5.0
        self.assertNotIn("curve=qsin", build_filter_graph(project, ProjectSettings()).filter_complex)

    def test_video_crossfade_audio_uses_equal_power_too(self):
        project = Project()
        project.add_media_asset(MediaAsset(path=SRC, has_video=True, has_audio=True))
        project.tracks[0].clips += [_video(0.0, transition_out=TransitionType.CROSSFADE), _video(4.0)]
        self.assertIn("curve=qsin", build_filter_graph(project, ProjectSettings()).filter_complex)

    def test_volume_keyframes_become_a_per_frame_envelope(self):
        project = Project()
        clip = _audio(0.0, volume_keyframes=[(0.0, 0.0), (2.0, 1.0)])
        project.tracks[1].clips.append(clip)
        fc = build_filter_graph(project, ProjectSettings()).filter_complex
        self.assertIn("volume=eval=frame:volume='if(lt(t,0.000000)", fc)

    def test_zero_volume_with_automation_is_still_audible(self):
        clip = _audio(0.0, volume=0.0, volume_keyframes=[(0.0, 0.0), (1.0, 1.0)])
        self.assertFalse(clip.is_silent)
        self.assertTrue(_audio(0.0, volume=0.0).is_silent)


class ColourHandlingTests(unittest.TestCase):  # B6
    def _graph_for(self, **asset_fields):
        project = Project()
        asset = MediaAsset(path=SRC, has_video=True, width=1920, height=1080, **asset_fields)
        project.add_media_asset(asset)
        project.tracks[0].clips.append(_video(0.0, asset_id=asset.id))
        return build_filter_graph(project, ProjectSettings()).filter_complex

    def test_hdr_source_is_tone_mapped_before_scaling(self):
        fc = self._graph_for(color_transfer="arib-std-b67", color_primaries="bt2020", color_space="bt2020nc")
        self.assertIn("zscale=tin=arib-std-b67:min=bt2020nc:pin=bt2020", fc)
        self.assertLess(fc.index("tonemap=tonemap=hable"), fc.index("scale=1920:1080"))

    def test_sdr_source_is_left_alone_and_sd_source_gets_bt709_matrix(self):
        self.assertNotIn("zscale", self._graph_for(color_transfer="bt709"))
        self.assertIn("in_color_matrix=bt601:out_color_matrix=bt709", self._graph_for(color_space="smpte170m"))

    def test_probe_fills_colour_fields(self):
        from core.media_probe import probe_file

        stream = {"codec_type": "video", "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                  "color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc",
                  "color_range": "tv", "avg_frame_rate": "30000/1001"}
        fake = subprocess.CompletedProcess([], 0, json.dumps({"format": {"duration": "5"}, "streams": [stream]}), "")
        with patch("core.media_probe.find_binary", return_value="ffprobe"), patch("core.media_probe.run", return_value=fake):
            asset = probe_file("/m/hdr.mov")
        self.assertTrue(asset.is_hdr)
        self.assertEqual(asset.bit_depth, 10)
        self.assertEqual(asset.color_range, "tv")


class EffectStackTests(unittest.TestCase):  # B7
    def test_pre_effects_run_before_alpha_post_effects_after_keying(self):
        project = Project()
        clip = _video(0.0, chroma_key_enabled=True, effects=[
            EffectInstance.create("sharpen", amount=1.5), EffectInstance.create("video_denoise", strength=3.0)])
        project.tracks[0].clips.append(clip)
        fc = build_filter_graph(project, ProjectSettings()).filter_complex
        self.assertLess(fc.index("hqdn3d=3.000"), fc.index("format=yuva420p"))
        self.assertLess(fc.index("colorkey"), fc.index("unsharp=5:5:1.500"))

    def test_disabled_unknown_and_fileless_effects_are_skipped(self):
        effects = [
            EffectInstance.create("blur", enabled=False),
            EffectInstance(kind="from_the_future", params={"x": 1}),
            EffectInstance.create("lut3d"),  # no file chosen yet
        ]
        effects[0].enabled = False
        self.assertEqual(effect_filters(effects, "post") + effect_filters(effects, "pre"), "")

    def test_params_are_clamped_and_round_trip(self):
        effect = EffectInstance.create("blur", sigma=999)
        self.assertEqual(effect.resolved_params()["sigma"], REGISTRY["blur"].params[0].maximum)
        clip = _video(0.0, effects=[effect, EffectInstance(kind="from_the_future", params={"x": 1})])
        restored = Clip.from_dict(json.loads(json.dumps(clip.to_dict())))
        self.assertEqual([e.kind for e in restored.effects], ["blur", "from_the_future"])

    def test_lut_path_is_escaped_for_the_filtergraph(self):
        effect = EffectInstance.create("lut3d", file="C:\\Looks\\it's.cube")
        # Same two-level escaping as the text overlay's textfile= path.
        self.assertIn(r"file=C\\:/Looks/it\\\'s.cube:interp", effect.filter())

    def test_missing_lut_file_fails_the_export_up_front(self):
        project = Project()
        clip = _video(0.0, effects=[EffectInstance.create("lut3d", file="/nowhere/look.cube")])
        project.tracks[0].clips.append(clip)
        open(SRC, "wb").close()
        with patch("core.exporter.find_binary", return_value="ffmpeg"):
            with self.assertRaises(ExportError) as ctx:
                Exporter(project).export(ExportSettings(output_path=os.path.join(tempfile.gettempdir(), "o.mp4")))
        self.assertIn("look.cube", str(ctx.exception))


class CodecTests(unittest.TestCase):  # PG1
    def test_codec_container_rules(self):
        self.assertEqual(resolve_codecs(".mp4"), ("libx264", "aac"))
        self.assertEqual(resolve_codecs(".mkv", "hevc"), ("libx265", "aac"))
        self.assertEqual(resolve_codecs(".webm", "av1"), ("libaom-av1", "libopus"))
        self.assertEqual(resolve_codecs(".mov", "prores"), ("prores_ks", "pcm_s16le"))
        with self.assertRaises(ValueError):
            resolve_codecs(".mp4", "prores")

    def test_quality_args(self):
        self.assertIn("hvc1", Exporter._video_quality_args("libx265", False, "23"))
        self.assertEqual(Exporter._video_quality_args("libaom-av1", False, "23")[1], "32")
        self.assertIn("dnxhr_hq", Exporter._video_quality_args("dnxhd", False, "23"))
        gpu = Exporter._video_quality_args("hevc_nvenc", True, "23")
        self.assertEqual(gpu[: len(NVENC_TUNING)], NVENC_TUNING)

    def test_hevc_nvenc_needs_its_own_probe(self):
        with patch("core.exporter.nvenc_available", return_value=True), \
                patch("core.exporter.nvenc_encoder_available", return_value=False):
            self.assertEqual(Exporter._resolve_video_codec("libx265", True), ("libx265", False))
        with patch("core.exporter.nvenc_available", return_value=True), \
                patch("core.exporter.nvenc_encoder_available", return_value=True):
            self.assertEqual(Exporter._resolve_video_codec("libaom-av1", True), ("av1_nvenc", True))

    def _export(self, settings, use_gpu=False):
        project = Project()
        project.add_media_asset(MediaAsset(path=SRC, duration=5.0, has_video=True))
        project.tracks[0].clips.append(_video(0.0, length=5.0))
        open(SRC, "wb").close()
        calls = []

        class Proc:
            def __init__(self, args):
                self.args, self.returncode, self.stdout = args, 0, io.StringIO("")
                out = args[-1]
                if out not in (os.devnull,):
                    open(out, "wb").close()

            def wait(self):
                return 0

            def poll(self):
                return 0

        def fake_popen(args, **kw):
            calls.append(list(args))
            return Proc(args)

        with patch("core.exporter.find_binary", return_value="ffmpeg"), patch("core.exporter.popen", fake_popen), \
                patch("core.exporter.nvenc_available", return_value=use_gpu), \
                patch("core.exporter.nvenc_encoder_available", return_value=use_gpu):
            Exporter(project).export(settings)
        return calls

    def test_outputs_are_tagged_bt709_and_ntsc_rate_is_exact(self):
        out = os.path.join(tempfile.gettempdir(), "ve_p2_out.mp4")
        calls = self._export(ExportSettings(output_path=out, fps=30000 / 1001, use_gpu=False))
        args = calls[0]
        self.assertEqual(args[args.index("-colorspace"): args.index("-colorspace") + 8], BT709_OUTPUT_TAGS)
        self.assertEqual(args[args.index("-r") + 1], "30000/1001")

    def test_two_pass_refuses_intermediate_codecs(self):
        out = os.path.join(tempfile.gettempdir(), "ve_p2_out.mov")
        with self.assertRaises(ExportError):
            self._export(ExportSettings(output_path=out, video_codec="prores", two_pass=True, target_size_mb=5))

    def test_gpu_target_size_is_one_capped_vbr_nvenc_pass(self):
        out = os.path.join(tempfile.gettempdir(), "ve_p2_out.mp4")
        calls = self._export(ExportSettings(output_path=out, two_pass=True, target_size_mb=5, use_gpu=True,
                                            gpu_target_size=True), use_gpu=True)
        self.assertEqual(len(calls), 1)
        self.assertIn("h264_nvenc", calls[0])
        self.assertIn("-maxrate", calls[0])
        self.assertNotIn("-pass", calls[0])

    def test_pcm_audio_has_no_bitrate(self):
        out = os.path.join(tempfile.gettempdir(), "ve_p2_out.mov")
        project_calls = self._export(ExportSettings(output_path=out, video_codec="prores"))
        self.assertIn("pcm_s16le", project_calls[0])
        self.assertNotIn("-b:a", project_calls[0])


class ExportUxHelpersTests(unittest.TestCase):  # F4
    def test_fps_expr(self):
        self.assertEqual(fps_expr(24000 / 1001), "24000/1001")
        self.assertEqual(fps_expr(59.94), "60000/1001")
        self.assertEqual(fps_expr(30.0), "30.0")

    def test_progress_message_reports_speed_and_time_left(self):
        fraction, message = progress_message("Rendering", 30.0, 120.0, 2.0, (0.0, 1.0))
        self.assertAlmostEqual(fraction, 0.25)
        self.assertIn("2.0x realtime, ~00:45 left", message)
        self.assertEqual(progress_message("Rendering", 30.0, 120.0, 0.0, (0.0, 1.0))[1], "Rendering... 25%")

    def test_user_presets_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ve_presets_"), "p.json")
        save_user_preset("Mine", {"container": ".mkv", "fps": 25.0}, path)
        self.assertEqual(load_user_presets(path)["Mine"]["container"], ".mkv")

    def test_source_defaults_come_from_the_most_used_asset(self):
        project = Project()
        small = MediaAsset(path="/m/s.mp4", width=1280, height=720, fps=25.0, has_video=True)
        big = MediaAsset(path="/m/b.mp4", width=3840, height=2160, fps=30000 / 1001, has_video=True)
        project.media_pool += [small, big]
        project.tracks[0].clips += [_video(0.0, 2.0, path=small.path, asset_id=small.id),
                                    _video(2.0, 8.0, path=big.path, asset_id=big.id)]
        self.assertIs(dominant_source(project), big)
        self.assertEqual(source_defaults(project), (3840, 2160, "29.97"))
        self.assertIsNone(nearest_fps_choice(17.0))


class MultiClipEditTests(unittest.TestCase):  # F6, F7
    def setUp(self):
        self.project = Project()
        self.v = self.project.tracks[0]
        self.a, self.b = _video(0.0), _video(4.0)
        self.v.clips += [self.a, self.b]
        self.history = _strict_history(self.project)

    def test_copy_paste_gives_fresh_ids_and_never_overlaps(self):
        payload = copy_clips(self.project, [self.a.id, self.b.id])
        self.history.execute(build_paste_command(self.project, json.loads(json.dumps(payload)), 2.0))
        clips = [c for t in self.project.tracks for c in t.clips]
        self.assertEqual(len(clips), 4)
        self.assertEqual(len({c.id for c in clips}), 4)
        self.assertEqual(self.project.validate(), [])
        self.history.undo()
        self.assertEqual(len([c for t in self.project.tracks for c in t.clips]), 2)

    def test_duplicate_lands_after_the_selection(self):
        self.history.execute(build_duplicate_command(self.project, [self.b.id]))
        starts = sorted(c.timeline_start for c in self.v.clips)
        self.assertEqual(starts, [0.0, 4.0, 8.0])

    def test_group_move_and_refusal(self):
        self.assertIsNone(build_move_clips_command(self.project, [self.a.id], 2.0))  # into b
        self.history.execute(build_move_clips_command(self.project, [self.a.id, self.b.id], 3.0))
        self.assertEqual((self.a.timeline_start, self.b.timeline_start), (3.0, 7.0))

    def test_multi_delete_with_ripple(self):
        c = _video(8.0)
        self.v.clips.append(c)
        self.history.execute(build_delete_clips_command(self.project, [self.a.id, self.b.id], ripple=True))
        self.assertEqual([x.id for x in self.v.clips], [c.id])
        self.assertEqual(c.timeline_start, 0.0)

    def test_paste_attributes(self):
        self.a.effects = [EffectInstance.create("blur")]
        self.a.brightness = 0.3  # a colour-correction effect in the same stack
        payload = copy_clips(self.project, [self.a.id])
        self.history.execute(build_paste_attributes_command(self.project, payload, [self.b.id]))
        self.assertEqual(self.b.brightness, 0.3)
        self.assertEqual([e.kind for e in self.b.effects], ["color_correction", "blur"])  # legacy order: eq first
        self.assertNotEqual(self.b.effects[1].id, self.a.effects[1].id)

    def test_insert_ripples_every_unlocked_track(self):
        music = _audio(2.0)
        self.project.tracks[1].clips.append(music)
        new = _video(2.0, length=3.0)
        self.history.execute(build_insert_command(self.project, new, self.v.id))
        self.assertEqual(self.project.validate(), [])
        self.assertEqual(sorted(round(c.timeline_start, 3) for c in self.v.clips), [0.0, 2.0, 5.0, 7.0])
        self.assertEqual(music.timeline_start, 5.0)

    def test_overwrite_replaces_what_is_underneath(self):
        new = _video(2.0, length=4.0)
        self.history.execute(build_overwrite_command(self.project, new, self.v.id))
        self.assertEqual(self.project.validate(), [])
        spans = sorted((round(c.timeline_start, 3), round(c.timeline_end, 3)) for c in self.v.clips)
        self.assertEqual(spans, [(0.0, 2.0), (2.0, 6.0), (6.0, 8.0)])

    def test_roll_moves_the_edit_point_only(self):
        self.b.source_in, self.b.source_out = 2.0, 6.0
        self.history.execute(build_roll_command(self.project, self.a.id, 5.0))
        self.assertEqual((self.a.timeline_end, self.b.timeline_start, self.b.timeline_end), (5.0, 5.0, 8.0))
        self.assertAlmostEqual(self.b.source_in, 3.0)

    def test_slip_keeps_timeline_place_and_clamps(self):
        self.history.execute(build_slip_command(self.project, self.b.id, 100.0))
        self.assertEqual((self.b.timeline_start, self.b.source_in, self.b.source_out), (4.0, 16.0, 20.0))

    def test_slide_trims_neighbours(self):
        c = _video(8.0)
        c.source_in, c.source_out = 2.0, 6.0
        self.v.clips.append(c)
        self.history.execute(build_slide_command(self.project, self.b.id, 1.0))
        self.assertEqual((self.a.timeline_end, self.b.timeline_start, c.timeline_start, c.timeline_end), (5.0, 5.0, 9.0, 12.0))
        self.assertEqual(self.project.validate(), [])

    def test_detach_audio_creates_linked_audio_clip(self):
        self.history.execute(build_detach_audio_command(self.project, self.a.id))
        audio = [c for c in self.project.tracks[1].clips]
        self.assertEqual(len(audio), 1)
        self.assertTrue(self.a.muted)
        self.assertEqual(set(linked_clip_ids(self.project, self.a.id)), {self.a.id, audio[0].id})
        self.history.undo()
        self.assertFalse(self.a.muted)
        self.assertEqual(self.project.tracks[1].clips, [])


class LeftTrimRebaseTests(unittest.TestCase):  # R1 leftover
    def test_left_trim_rebases_speed_and_opacity_keyframes(self):
        project = Project()
        clip = _video(0.0, length=4.0, speed_keyframes=[(0.0, 1.0), (2.0, 0.5)],
                      opacity_keyframes=[(0.0, 0.0), (2.0, 1.0)])
        project.tracks[0].clips.append(clip)
        history = History()
        history.execute(TrimClipCommand(project, clip.id, 1.0, 4.0, 1.0))
        self.assertEqual(clip.speed_keyframes, [(0.0, 1.0), (1.0, 0.5)])
        self.assertEqual(clip.opacity_keyframes[0], (0.0, 0.5))
        self.assertAlmostEqual(clip.duration, 1.0 + 2.0 / 0.5)
        history.undo()
        self.assertEqual(clip.speed_keyframes, [(0.0, 1.0), (2.0, 0.5)])


class KeyframePointTests(unittest.TestCase):  # F12
    def test_keyframe_points_span_all_animated_properties(self):
        clip = _video(10.0, opacity_keyframes=[(0.0, 1.0), (1.0, 0.0)], volume_keyframes=[(2.0, 1.0), (3.0, 0.5)],
                      speed_keyframes=[(0.0, 1.0), (2.0, 2.0)])
        self.assertEqual(clip_keyframe_points(clip), [10.0, 11.0, 12.0, 13.0])


class AutosaveTests(unittest.TestCase):  # R2
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ve_autosave_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_generations_rotate(self):
        session = AutosaveSession(self.dir)
        for n in range(5):
            session.write({"n": n}, {"name": "p"})
        self.assertEqual(len(session.generations()), 4)  # current + 3 kept
        with open(session.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["n"], 4)

    def test_only_dead_sessions_are_offered(self):
        live = AutosaveSession(self.dir)
        live.write({}, {"name": "live"})
        dead = AutosaveSession(self.dir, pid=999_999_999)
        dead.write({}, {"name": "crashed"})
        orphans = find_orphaned_autosaves(self.dir)
        self.assertEqual([o.name for o in orphans], ["crashed"])
        orphans[0].discard()
        self.assertEqual(find_orphaned_autosaves(self.dir), [])
        live.close()
        self.assertEqual(os.listdir(self.dir), [])

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(999_999_999))


class TimeoutTests(unittest.TestCase):  # R5
    def test_run_has_a_default_timeout(self):
        from core import ffmpeg_utils

        with patch("core.ffmpeg_utils.subprocess.run") as fake:
            ffmpeg_utils.run(["x"])
        self.assertEqual(fake.call_args.kwargs["timeout"], ffmpeg_utils.DEFAULT_TIMEOUT_S)

    def test_probe_timeout_becomes_a_probe_error_and_thumbnail_none(self):
        from core import media_probe

        boom = subprocess.TimeoutExpired(["ffprobe"], 30)
        with patch("core.media_probe.find_binary", return_value="ffprobe"), \
                patch("core.media_probe.run", side_effect=boom):
            with self.assertRaises(media_probe.MediaProbeError):
                media_probe.probe_file("/m/a.mp4")
            self.assertIsNone(media_probe.generate_thumbnail("/m/a.mp4", tempfile.mkdtemp(prefix="ve_th_")))

    def test_failed_export_time_probe_is_reported_as_a_warning(self):
        project = Project()
        project.tracks[0].clips.append(_video(0.0, path="/m/not_in_pool.mp4"))
        with patch("core.media_probe.probe_file", side_effect=RuntimeError("hung")):
            graph = build_filter_graph(project, ProjectSettings())
        self.assertEqual(len(graph.warnings), 1)
        self.assertIn("not_in_pool", graph.warnings[0])

    def test_safe_thumbnail_time(self):
        from core.media_probe import safe_thumbnail_time

        self.assertEqual(safe_thumbnail_time(0.4), 0.2)
        self.assertEqual(safe_thumbnail_time(10.0), 1.0)


if __name__ == "__main__":
    unittest.main()
