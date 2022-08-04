import superdesk

from typing import Dict

from . import privileges, types

from apps.archive.common import ITEM_DUPLICATE
from superdesk.metadata.item import metadata_schema


class RundownItemsResource(superdesk.Resource):
    schema = {
        "title": metadata_schema["headline"],
        "item_type": superdesk.Resource.not_analyzed_field(required=True),
        "content": metadata_schema["body_html"],
        "duration": {
            "type": "number",
        },
        "planned_duration": {
            "type": "number",
        },
        "operation": superdesk.Resource.not_analyzed_field(),
        "original_id": superdesk.Resource.not_analyzed_field(),
        "show_part": superdesk.Resource.not_analyzed_field(),
        "live_sound": superdesk.Resource.not_analyzed_field(),
        "guests": superdesk.Resource.not_analyzed_field(),
        "additional_notes": superdesk.Resource.not_analyzed_field(),
        "live_captions": superdesk.Resource.not_analyzed_field(),
        "last_sentence": superdesk.Resource.not_analyzed_field(),
    }

    datasource = {
        "search_backend": "elastic",
    }

    versioning = True
    privileges = {"POST": privileges.RUNDOWNS, "PATCH": privileges.RUNDOWNS, "PUT": privileges.RUNDOWNS}


class RundownItemsService(superdesk.Service):
    def create_from_template(self, template: types.IRundownItemTemplate) -> types.IRundownItem:
        item: types.IRundownItem = {
            "item_type": template["item_type"],
            "title": template.get("title"),
            "duration": template.get("duration"),
            "planned_duration": template.get("planned_duration"),
        }

        self.create([item])
        return item

    def get_durations(self, refs: types.IRefs) -> int:
        durations = {}
        cursor = self.get_from_mongo(
            req=None, lookup={"_id": {"$in": [ref["_id"] for ref in refs]}}, projection={"duration": 1}
        )
        for item in cursor:
            durations[item["_id"]] = item["duration"]
        duration = 0
        for ref in refs:
            duration += durations[ref["_id"]]
        return duration


items_service = RundownItemsService()
