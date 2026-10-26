# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

import hashlib
from datetime import datetime, timezone

import frappe
from frappe.utils import get_datetime

CHECKPOINT_DOCTYPE = "Shipstation Poll Checkpoint"
SHIPMENT_PIPELINE = "shipment"
ORDER_PIPELINE = "order"
PIPELINE_FIELDS = {
	SHIPMENT_PIPELINE: "shipment_checkpoint",
	ORDER_PIPELINE: "order_checkpoint",
}
MAX_SAVE_ATTEMPTS = 3


def checkpoint_name(settings_name: str | None, store_id: str | None) -> str:
	key = f"{settings_name or ''}|{store_id or ''}".encode("utf-8")
	return "poll-" + hashlib.sha256(key).hexdigest()[:20]


def pipeline_field(pipeline: str) -> str:
	field = PIPELINE_FIELDS.get(pipeline)
	if not field:
		raise frappe.ValidationError(f"Unknown ShipStation polling pipeline: {pipeline}")
	return field


def to_naive_utc(value: datetime | str | None) -> datetime | None:
	if value is None:
		return None
	if isinstance(value, str):
		value = get_datetime(value)
	if value.tzinfo is not None:
		value = value.astimezone(timezone.utc).replace(tzinfo=None)
	return value


def get_checkpoint(settings_name: str | None, store_id: str | None) -> frappe._dict | None:
	name = checkpoint_name(settings_name, store_id)
	if not frappe.db.exists(CHECKPOINT_DOCTYPE, name):
		return None
	values = frappe.db.get_value(
		CHECKPOINT_DOCTYPE,
		name,
		["settings", "store_id", *PIPELINE_FIELDS.values()],
		as_dict=True,
	)
	return frappe._dict(name=name, **values)


def read_checkpoint(
	settings_name: str | None, store_id: str | None, pipeline: str
) -> datetime | None:
	checkpoint = get_checkpoint(settings_name, store_id)
	if not checkpoint:
		return None
	return to_naive_utc(checkpoint.get(pipeline_field(pipeline)))


def advance_checkpoint(
	settings_name: str | None, store_id: str | None, pipeline: str, value: datetime
) -> bool:
	field = pipeline_field(pipeline)
	target = to_naive_utc(value)
	if target is None:
		return False
	name = checkpoint_name(settings_name, store_id)

	for _attempt in range(MAX_SAVE_ATTEMPTS):
		if frappe.db.exists(CHECKPOINT_DOCTYPE, name):
			doc = frappe.get_doc(CHECKPOINT_DOCTYPE, name)
			doc.flags.ignore_links = True
			current = to_naive_utc(doc.get(field))
			if current is not None and current >= target:
				return False

			doc.set(field, target)
			try:
				doc.save(ignore_permissions=True)
			except frappe.TimestampMismatchError:
				continue
			except frappe.DuplicateEntryError:
				continue
		else:
			doc = _new_checkpoint_doc(settings_name, store_id)
			doc.set(field, target)
			try:
				doc.insert(ignore_permissions=True)
			except frappe.DuplicateEntryError:
				continue

		frappe.db.commit()
		return True

	frappe.log_error(
		title="ShipStation poll checkpoint could not be advanced",
		message=(
			f"The {pipeline} checkpoint of store {store_id} could not be written because the "
			"record was modified concurrently. The window is polled again by the next run."
		),
	)
	return False


def _new_checkpoint_doc(settings_name: str | None, store_id: str | None):
	doc = frappe.new_doc(CHECKPOINT_DOCTYPE)
	doc.store_id = str(store_id or "")
	doc.settings = settings_name or None
	doc.flags.ignore_links = True
	return doc
