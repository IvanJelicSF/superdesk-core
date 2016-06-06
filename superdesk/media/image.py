# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

'''
Utilities for extractid metadata from image files.
'''

from PIL import Image, ExifTags
from flask import json

ORIENTATIONS = {
    1: ("Normal", 0),
    2: ("Mirrored left-to-right", 0),
    3: ("Rotated 180 degrees", 180),
    4: ("Mirrored top-to-bottom", 0),
    5: ("Mirrored along top-left diagonal", 0),
    6: ("Rotated 90 degrees", -90),
    7: ("Mirrored along top-right diagonal", 0),
    8: ("Rotated 270 degrees", -270)
}
EXIF_ORIENTATION_TAG = 274


def fix_orientation(file_stream):
    '''
    Returns the image fixed accordingly to the orientation.
​
    @param file_stream: stream
    '''
    file_stream.seek(0)
    img = Image.open(file_stream)
    if not hasattr(img, '_getexif'):
        return
    rv = img._getexif()
    if not rv:
        return
    exif = dict(rv)
    orientation = exif[EXIF_ORIENTATION_TAG]
    if orientation in [3, 6, 8]:
        degrees = ORIENTATIONS[orientation][1]
        img2 = img.rotate(degrees)
        file_stream.truncate(0)
        file_stream.seek(0)
        img2.save(file_stream, 'jpeg')
        file_stream.seek(0)


def get_meta(file_stream):
    '''
    Returns the image metadata in a dictionary of tag:value pairs.

    @param file_stream: stream
    '''
    current = file_stream.tell()
    file_stream.seek(0)
    img = Image.open(file_stream)
    if not hasattr(img, '_getexif'):
        return {}
    rv = img._getexif()
    if not rv:
        return {}
    exif = dict(rv)
    file_stream.seek(current)

    exif_meta = {}
    for k, v in exif.items():
        try:
            json.dumps(v)
            key = ExifTags.TAGS[k].strip()

            if key == 'GPSInfo':
                # lookup GPSInfo description key names
                value = {ExifTags.GPSTAGS[vk].strip(): vv for vk, vv in v.items()}
                exif_meta[key] = value
            else:
                value = v.decode('UTF-8') if isinstance(v, bytes) else v
                exif_meta[key] = value
        except:
            # ignore fields we can't store in db
            pass
    # Remove this as it's too long to send in headers
    exif_meta.pop('UserComment', None)

    return exif_meta
