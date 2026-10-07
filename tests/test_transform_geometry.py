"""F15: on-canvas transform geometry (core.transform_geometry)."""
import math
import unittest

from core.transform_geometry import MAX_SCALE, MIN_SCALE, clip_box, moved, scaled


class ClipBoxTests(unittest.TestCase):
    def test_a_fitted_clip_fills_the_canvas_height_or_width(self):
        box = clip_box(0.5, 0.5, 1.0, 0.0, (1280, 720), (1920, 1080))
        self.assertEqual((box.cx, box.cy, box.width, box.height), (960, 540, 1920, 1080))
        portrait = clip_box(0.5, 0.5, 1.0, 0.0, (1080, 1920), (1920, 1080))
        self.assertAlmostEqual(portrait.height, 1080)
        self.assertAlmostEqual(portrait.width, 1080 * 1080 / 1920)

    def test_scale_position_and_rotation(self):
        box = clip_box(0.25, 0.75, 0.5, 90.0, (1920, 1080), (1920, 1080))
        self.assertEqual((box.cx, box.cy, box.width, box.height), (480, 810, 960, 540))
        corners = box.corners()
        # Rotated 90 degrees clockwise: the top-left corner ends up top-right of the centre.
        self.assertAlmostEqual(corners[0][0], 480 + 270)
        self.assertAlmostEqual(corners[0][1], 810 - 480)
        self.assertTrue(box.contains(480 + 200, 810))
        self.assertFalse(box.contains(480 + 400, 810))  # inside the unrotated box, outside the rotated one

    def test_moving_stays_on_the_canvas(self):
        self.assertEqual(moved(0.5, 0.5, 192, -108, (1920, 1080)), (0.6, 0.4))
        self.assertEqual(moved(0.9, 0.1, 1000, -1000, (1920, 1080)), (1.0, 0.0))

    def test_corner_drag_scales_by_distance_from_the_centre(self):
        box = clip_box(0.5, 0.5, 1.0, 0.0, (1920, 1080), (1920, 1080))
        start = box.corners()[2]
        self.assertAlmostEqual(scaled(box, 1.0, start, (960 + (start[0] - 960) / 2, 540 + (start[1] - 540) / 2)), 0.5)
        self.assertEqual(scaled(box, 1.0, start, (10_000, 10_000)), MAX_SCALE)
        self.assertEqual(scaled(box, 1.0, start, (960, 540)), MIN_SCALE)
        self.assertEqual(scaled(box, 1.0, (960, 540), start), 1.0)  # no distance to measure from
        self.assertTrue(math.isfinite(scaled(box, 1.0, start, start)))


if __name__ == "__main__":
    unittest.main()
