# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

"""Utilities for extractid metadata from image files."""

import io
import logging

from typing import BinaryIO, Dict, List, Union

from superdesk.text_utils import decode
from PIL import Image, ExifTags, ImageOps
from PIL import IptcImagePlugin
from PIL.TiffImagePlugin import IFDRational
from flask import json
from superdesk.media.metadata_mapping import MediaMetadata

from .iim_codes import iim_codes

logger = logging.getLogger(__name__)

try:
    import pyexiv2
except ImportError:
    logging.warning("pyexiv2 is not installed, writing picture metadata will not work")
    pass


EXIF_ORIENTATION_TAG = 274


def fix_orientation(file_stream):
    """Return the image with EXIF Orientation baked into the pixels.

    Applies the EXIF Orientation transform (rotate/flip) to the pixel
    data and strips the tag, so downstream cropping/resizing works on
    upright pixels. Returns the original stream unchanged if no
    correction is needed.

    @param file_stream: stream
    """
    file_stream.seek(0)
    img = Image.open(file_stream)
    try:
        exif = img.getexif()
    except AttributeError:
        exif = None
    orientation = exif.get(EXIF_ORIENTATION_TAG) if exif else None
    if not orientation or orientation == 1:
        file_stream.seek(0)
        return file_stream

    transposed = ImageOps.exif_transpose(img)
    fmt = img.format or "JPEG"
    output = io.BytesIO()
    try:
        if fmt.upper() == "JPEG" and transposed.mode != "RGB":
            transposed = transposed.convert("RGB")
        transposed.save(output, fmt, quality=95)
    except (IOError, OSError):
        output = io.BytesIO()
        transposed.convert("RGB").save(output, fmt, quality=95)
    output.seek(0)
    return output


def get_meta(file_stream):
    """Returns the image metadata in a dictionary of tag:value pairs.

    @param file_stream: stream
    """
    current = file_stream.tell()
    file_stream.seek(0)
    img = Image.open(file_stream)
    try:
        rv = img.getexif()
    except AttributeError:
        return {}
    if not rv:
        return {}
    exif = dict(rv)
    file_stream.seek(current)

    exif_meta = {}
    for k, v in exif.items():
        try:
            key = ExifTags.TAGS[k].strip()
        except KeyError:
            continue

        if key == "GPSInfo":
            # lookup GPSInfo description key names
            value = {
                ExifTags.GPSTAGS[vk].strip(): convert_exif_value(vv, vk)
                for vk, vv in rv.get_ifd(k).items()
                if is_serializable(vv)
            }
            exif_meta[key] = value
        elif is_serializable(v):
            value = v.decode("UTF-8") if isinstance(v, bytes) else v
            exif_meta[key] = convert_exif_value(value)

    # Remove this as it's too long to send in headers
    exif_meta.pop("UserComment", None)

    return exif_meta


def convert_exif_value(val, key=None):
    if ExifTags.GPSTAGS.get(key) == "GPSAltitudeRef":
        return 0 if val == b"\x00" else 1
    if isinstance(val, tuple):
        return tuple([convert_exif_value(v) for v in val])
    if isinstance(val, list):
        return list([convert_exif_value(v) for v in val])
    if isinstance(val, IFDRational):
        try:
            return float(str(val._val))
        except ValueError:
            numerator, denominator = val.limit_rational(100)
            return round(numerator / denominator, 3)
    return val


def is_serializable(val):
    try:
        json.dumps(convert_exif_value(val))
    except (TypeError, ValueError):
        return False
    return True


def get_meta_iptc(file_stream: BinaryIO):
    """Returns the image IPTC metadata in a dictionary of tag:value pairs.

    @param file_stream: stream
    """
    file_stream.seek(0)
    img = Image.open(file_stream)
    iptc_raw = IptcImagePlugin.getiptcinfo(img)
    metadata: Dict[str, Union[str, List[str]]] = {}

    if iptc_raw is None:
        return metadata

    for code, value in iptc_raw.items():
        try:
            tag = iim_codes[code]
        except KeyError:
            continue
        if isinstance(value, list):
            value = [decode(v) for v in value]
        elif isinstance(value, bytes):
            value = decode(value)
        metadata[tag] = value
    return metadata


def read_metadata(input: bytes) -> MediaMetadata:
    """Reads the metadata from the image file.

    @param file_stream: stream
    """
    with pyexiv2.ImageData(input) as img:
        xmp = img.read_xmp()
    return {
        "Description": get_xmp_lang_string(xmp.get("Xmp.dc.description")),
        "DescriptionWriter": xmp.get("Xmp.photoshop.CaptionWriter", ""),
        "Headline": xmp.get("Xmp.photoshop.Headline", ""),
        "Instructions": xmp.get("Xmp.photoshop.Instructions", ""),
        "JobId": xmp.get("Xmp.photoshop.TransmissionReference", ""),
        "Title": get_xmp_lang_string(xmp.get("Xmp.dc.title")),
        "Creator": xmp.get("Xmp.dc.creator", []),
        "CreatorsJobtitle": xmp.get("Xmp.photoshop.AuthorsPosition", ""),
        "CopyrightNotice": get_xmp_lang_string(xmp.get("Xmp.dc.rights", "")),
        "City": xmp.get("Xmp.photoshop.City", ""),
        "Country": xmp.get("Xmp.photoshop.Country", ""),
        "CountryCode": xmp.get("Xmp.iptc.CountryCode", ""),
        "CreditLine": xmp.get("Xmp.photoshop.Credit", ""),
        "ProvinceState": xmp.get("Xmp.photoshop.State", ""),
    }


def get_xmp_lang_string(value, lang="x-default"):
    lang_key = 'lang="{}"'.format(lang)
    if value and isinstance(value, dict) and value.get(lang_key):
        return value[lang_key]
    if value and isinstance(value, str):
        return value
    return ""


def write_metadata(input: bytes, metadata: MediaMetadata) -> bytes:
    """Writes the metadata to the image file.

    @param file_stream: stream
    @param metadata: dict
    """
    from pyexiv2 import convert_xmp_to_iptc

    xmp = {
        "Xmp.dc.description": metadata.get("Description"),
        "Xmp.photoshop.CaptionWriter": metadata.get("DescriptionWriter"),
        "Xmp.photoshop.Headline": metadata.get("Headline"),
        "Xmp.photoshop.Instructions": metadata.get("Instructions"),
        "Xmp.photoshop.TransmissionReference": metadata.get("JobId"),
        "Xmp.dc.title": metadata.get("Title"),
        "Xmp.dc.creator": metadata.get("Creator"),
        "Xmp.photoshop.AuthorsPosition": metadata.get("CreatorsJobtitle"),
        "Xmp.dc.rights": metadata.get("CopyrightNotice"),
        "Xmp.photoshop.City": metadata.get("City"),
        "Xmp.photoshop.Country": metadata.get("Country"),
        "Xmp.iptc.CountryCode": metadata.get("CountryCode"),
        "Xmp.photoshop.Credit": metadata.get("CreditLine"),
        "Xmp.photoshop.State": metadata.get("ProvinceState"),
    }

    xmp = {k: v for k, v in xmp.items() if v}
    iptc = convert_xmp_to_iptc(xmp)

    with pyexiv2.ImageData(input) as img:
        img.modify_xmp(xmp)
        img.modify_iptc(iptc)
        return img.get_bytes()
