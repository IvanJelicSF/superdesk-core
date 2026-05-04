# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license


import io
import os
from unittest import TestCase
from PIL import Image
from superdesk.media.image import EXIF_ORIENTATION_TAG, fix_orientation, get_meta
from superdesk.media.media_operations import crop_image


fixtures = os.path.join(os.path.abspath(os.path.dirname(__file__)), "fixtures")


class ExifMetaExtractionTestCase(TestCase):
    img = os.path.join(fixtures, "canon_exif.JPG")

    def test_extract_meta_json_serialization(self):
        with open(self.img, mode="rb") as f:
            meta = get_meta(f)

        self.assertEqual(meta["Make"], "Canon")
        self.assertEqual(meta["Model"], "Canon EOS 60D")


class ExifMetaWithGPSInfoExtractionTestCase(TestCase):
    img = os.path.join(fixtures, "iphone_gpsinfo_exif.JPG")
    maxDiff = None

    def test_extract_meta_json_serialization(self):
        expected_gpsinfo = {
            "GPSImgDirection": 31.145,
            "GPSLongitudeRef": "W",
            "GPSImgDirectionRef": "T",
            "GPSAltitudeRef": 0,
            "GPSLatitudeRef": "S",
            "GPSAltitude": 576.978,
            "GPSLatitude": (33.0, 26.0, 11.5),
            "GPSLongitude": (70.0, 38.0, 39.46),
            "GPSTimeStamp": (19.0, 59.0, 51.17),
        }

        with open(self.img, mode="rb") as f:
            meta = get_meta(f)

        self.assertEqual(meta.get("Make", None), "Apple")
        self.assertEqual(meta.get("Model", None), "iPhone 5")
        self.assertEqual(meta.get("GPSInfo", None), expected_gpsinfo)


class ExifMetaExtractionUserCommentRemovedTestCase(TestCase):
    # this image has UserComment exif data value as 'test'
    img = os.path.join(fixtures, "iphone_gpsinfo_exif.JPG")

    def test_extract_meta_json_serialization(self):
        with open(self.img, mode="rb") as f:
            meta = get_meta(f)

        self.assertIsNone(meta.get("UserComment", None))


class MediaOperationsTestCase(TestCase):
    img = os.path.join(fixtures, "canon_exif.JPG")

    crop_data = {
        "CropLeft": 1,
        "CropTop": 1,
        "CropRight": 9,
        "CropBottom": 7,
    }

    def test_crop_image(self):
        with open(self.img, mode="rb") as f:
            status, output = crop_image(f, "test", self.crop_data)
            self.assertEqual(True, status)
            self.assertEqual(8, output.width)
            self.assertEqual(6, output.height)

    def test_crop_image_resize(self):
        with open(self.img, mode="rb") as f:
            status, output = crop_image(f, "test", self.crop_data, {"width": 4, "height": 3})
            self.assertEqual(True, status)
            self.assertEqual(4, output.width)
            self.assertEqual(3, output.height)


def _make_image_with_orientation(orientation_tag, fmt="JPEG"):
    """Build a 200x100 image with a red mark at the top-left of the stored
    pixels and the given EXIF Orientation tag."""
    img = Image.new("RGB", (200, 100), "white")
    for x in range(0, 50):
        for y in range(0, 25):
            img.putpixel((x, y), (255, 0, 0))
    exif = img.getexif()
    exif[EXIF_ORIENTATION_TAG] = orientation_tag
    buf = io.BytesIO()
    img.save(buf, fmt, exif=exif)
    buf.seek(0)
    return buf


class FixOrientationTestCase(TestCase):
    # For each orientation N, where the red top-left of the stored pixels
    # ends up after exif_transpose corrects the image.
    expected_red_corner = {
        1: "top-left",
        2: "top-right",
        3: "bottom-right",
        4: "bottom-left",
        5: "top-left",
        6: "top-right",
        7: "bottom-right",
        8: "bottom-left",
    }

    def _find_red_corner(self, img):
        w, h = img.size
        samples = {
            "top-left": img.getpixel((5, 5)),
            "top-right": img.getpixel((w - 6, 5)),
            "bottom-left": img.getpixel((5, h - 6)),
            "bottom-right": img.getpixel((w - 6, h - 6)),
        }
        for corner, pixel in samples.items():
            r, g, b = pixel[:3]
            if r > 200 and g < r - 20 and b < r - 20:
                return corner
        return None

    def test_all_orientations_corrected(self):
        for orient, expected in self.expected_red_corner.items():
            with self.subTest(orientation=orient):
                buf = _make_image_with_orientation(orient)
                result = fix_orientation(buf)
                result_img = Image.open(result)
                self.assertEqual(expected, self._find_red_corner(result_img))
                # Tag is removed (or normalized to 1) so downstream consumers
                # don't apply the transform a second time.
                self.assertIn(result_img.getexif().get(EXIF_ORIENTATION_TAG), (None, 1))

    def test_no_op_for_orientation_one(self):
        buf = _make_image_with_orientation(1)
        original_bytes = buf.getvalue()
        result = fix_orientation(buf)
        self.assertEqual(original_bytes, result.getvalue())

    def test_no_op_when_no_exif(self):
        img = Image.new("RGB", (200, 100), "white")
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        buf.seek(0)
        original_bytes = buf.getvalue()
        result = fix_orientation(buf)
        self.assertEqual(original_bytes, result.getvalue())

    def test_png_format_preserved(self):
        buf = _make_image_with_orientation(3, fmt="PNG")
        result = fix_orientation(buf)
        result_img = Image.open(result)
        self.assertEqual("PNG", result_img.format)
        self.assertEqual("bottom-right", self._find_red_corner(result_img))
