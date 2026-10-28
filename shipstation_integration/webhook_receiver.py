# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

import hmac
import json
import re
from urllib.parse import parse_qs, urlparse

import frappe
from frappe import _
from frappe.utils import getdate
from shipstation.models import ShipStationOrder

from shipstation_integration.orders import validate_order
from shipstation_integration.shipstation import orders_v1, shipments_adapter, shipments_v1


@frappe.whitelist(allow_guest=True)
def shipstation_webhook():
	if frappe.local.form_dict:
		data = frappe.local.form_dict
	elif hasattr(frappe.local, "request") and getattr(frappe.local.request, "data", None):
		data = json.loads(frappe.local.request.data)
	else:
		data = {}

	resource_type = data.get("resource_type")
	resource_url = data.get("resource_url")

	parsed_url = urlparse(resource_url or "")
	parsed_qs = parse_qs(parsed_url.query)
	callback_params = {k: v[0] for k, v in parsed_qs.items() if v}
	store_id = callback_params.get("storeID") or callback_params.get("storeId")

	if not resource_url or not re.fullmatch(r"ssapi\d*\.shipstation\.com", parsed_url.hostname or ""):
		frappe.throw(_("Invalid resource URL host"), frappe.PermissionError)

	token = (
		frappe.form_dict.get("token")
		or (frappe.request.args.get("token") if getattr(frappe, "request", None) else None)
		or data.get("token")
	)

	if resource_type in ["SHIP_NOTIFY", "ITEM_SHIP_NOTIFY"]:
		store = resolve_shipment_store(store_id, token=token)
		if not store:
			return
		sss_doc = frappe.get_doc("Shipstation Settings", store.parent)
	elif resource_type == "ORDER_NOTIFY":
		store = resolve_order_store(store_id, token=token)
		if not store:
			return
		sss_doc = frappe.get_doc("Shipstation Settings", store.parent)
	else:
		return

	secret = sss_doc.get_password("webhook_secret", raise_exception=False)
	if not secret:
		frappe.throw(_("Webhook secret is not configured"), frappe.PermissionError)

	if not token or not hmac.compare_digest(str(token), str(secret)):
		frappe.throw(_("Invalid webhook token"), frappe.PermissionError)

	if resource_type in ["SHIP_NOTIFY", "ITEM_SHIP_NOTIFY"]:
		new_flow_applies = bool(store_id and store and store.store_id == store_id and store.track_shipments)
		legacy_flow_applies = bool(
			store
			and store.enable_shipments
			and any(
				[
					store.create_sales_invoice,
					store.create_delivery_note,
					store.create_shipment,
				]
			)
		)
		if not (new_flow_applies or legacy_flow_applies):
			return
	elif resource_type == "ORDER_NOTIFY":
		if not (store and store.enable_orders):
			return

	if not sss_doc.enabled or not sss_doc.enable_legacy_api:
		return

	client = sss_doc.client()

	if resource_type == "ORDER_NOTIFY":
		if not store or not store.enable_orders:
			return

		orders = orders_v1.fetch_order_notify_orders(
			client=client,
			store_id=store.store_id,
			resource_url=resource_url,
			callback_params=callback_params,
		)
		for order in orders:
			ss_order = ShipStationOrder().json(order)

			if not validate_order(sss_doc, ss_order, store):
				continue

			should_create_order = True

			process_order_hook = frappe.get_hooks("process_shipstation_order")
			if process_order_hook:
				should_create_order = frappe.get_attr(process_order_hook[0])(ss_order, store)

			if not should_create_order:
				continue

			frappe.enqueue(
				method="shipstation_integration.orders.create_order_from_webhook",
				queue="shipstation",
				job_id=f"create_order_from_webhook_{order.get('orderId')}",
				order=order,
				store=store.name,
				settings=sss_doc.name,
			)

	elif resource_type in ["SHIP_NOTIFY", "ITEM_SHIP_NOTIFY"]:
		query = shipments_v1.build_shipments_query(store.store_id, callback_params)
		shipments = shipments_v1.fetch_shipments(client, query)

		processed = set()
		for shipment in shipments:
			norm = shipments_adapter.from_api_dict(shipment)
			shipment_id = norm.get("shipment_id") or f"ORDER-{norm.get('order_id')}"
			voided_flag = 1 if norm.get("voided") else 0
			shipment_key = (shipment_id, voided_flag)
			if shipment_key in processed:
				continue
			processed.add(shipment_key)

			if store.track_shipments:
				frappe.enqueue(
					method="shipstation_integration.shipstation.shipments_inbound.process_shipment",
					queue="default",
					job_id=f"process_shipment_{shipment_id}_{voided_flag}",
					deduplicate=True,
					shipment=norm,
					source="Webhook",
				)

			if store and store.enable_shipments and any(
				[
					store.create_sales_invoice,
					store.create_delivery_note,
					store.create_shipment,
				]
			):
				ss_shipment = ShipStationOrder().json(shipment)

				if sss_doc.since_date and getdate(ss_shipment.create_date) < sss_doc.since_date:
					continue

				if (
					frappe.db.exists(
						"Delivery Note",
						{"docstatus": 1, "shipstation_order_id": ss_shipment.order_id},
					)
					and not shipment.get("voided")
				):
					continue

				frappe.enqueue(
					method="shipstation_integration.shipments.create_shipment_from_webhook",
					queue="shipstation",
					job_id=f"create_shipment_from_webhook_{shipment.get('shipmentId')}",
					shipment=shipment,
					store=store.name,
					settings=sss_doc.name,
				)

	return


def resolve_store(store_id: str | None, resource_type: str = "SHIP_NOTIFY", token: str | None = None):
	if not store_id:
		frappe.log_error(
			title=f"ShipStation {resource_type} callback without store id",
			message=(
				f"The {resource_type} callback did not include a store id. Processing was "
				"stopped because the lookup cannot be restricted to a store. "
				"Subscribe the webhook per store in ShipStation."
			),
		)
		return None

	if not str(store_id).isdigit():
		frappe.log_error(
			title=f"ShipStation {resource_type} callback with invalid store id",
			message=(
				f"Store id {store_id!r} is not a valid ShipStation store id. "
				"Processing was stopped."
			),
		)
		return None

	store_records = frappe.get_all(
		"Shipstation Store",
		filters={"store_id": str(store_id)},
		fields=["name", "parent"],
	)
	if not store_records:
		frappe.log_error(
			title=f"ShipStation {resource_type} callback with unknown store id",
			message=(
				f"Store id {store_id!r} is not configured in Shipstation Settings. "
				"Processing was stopped."
			),
		)
		return None

	target_store_name = None
	if len(store_records) == 1:
		target_store_name = store_records[0].name
	elif token:
		for rec in store_records:
			if rec.parent and frappe.db.exists("Shipstation Settings", rec.parent):
				parent_doc = frappe.get_doc("Shipstation Settings", rec.parent)
				sec = parent_doc.get_password("webhook_secret", raise_exception=False)
				if sec and hmac.compare_digest(str(token), str(sec)):
					target_store_name = rec.name
					break

	if not target_store_name:
		frappe.log_error(
			title=f"ShipStation {resource_type} callback store cannot be associated with settings",
			message=(
				f"Store id {store_id!r} matches multiple or unassociated store configurations and could "
				"not be resolved with the provided credentials. Processing was stopped."
			),
		)
		return None

	store = frappe.get_doc("Shipstation Store", target_store_name)
	if not store.parent or not frappe.db.exists("Shipstation Settings", store.parent):
		frappe.log_error(
			title=f"ShipStation {resource_type} callback store without settings",
			message=(
				f"Store {target_store_name} (store id {store_id!r}) is not associated with a valid "
				"Shipstation Settings record. Processing was stopped."
			),
		)
		return None

	return store


def resolve_shipment_store(store_id: str | None, token: str | None = None):
	return resolve_store(store_id, resource_type="SHIP_NOTIFY", token=token)


def resolve_order_store(store_id: str | None, token: str | None = None):
	return resolve_store(store_id, resource_type="ORDER_NOTIFY", token=token)


@frappe.whitelist(allow_guest=True)
def shipstation_api_webhook():
	"""
	Handle ShipStation API v2 webhooks.

	Supported events:
	- batch: Batch label processing completion
	- track: Tracking updates
	- carrier_connected: New carrier connected
	- label_created: Label creation notification
	- sales_order_status_change: Order status changes
	"""
	try:
		data = frappe.local.form_dict or json.loads(frappe.local.request.data)
	except Exception:
		frappe.log_error(
			title="ShipStation API Webhook Error",
			message="Failed to parse webhook payload",
		)
		return {"status": "error", "message": "Invalid payload"}

	event_type = data.get("event") or data.get("resource_type")

	if not event_type:
		return {"status": "ignored", "message": "No event type specified"}

	# Log the webhook for debugging
	frappe.logger("shipstation").debug(
		f"Webhook received: {event_type}\n{json.dumps(data, indent=2)}"
	)

	# Route to appropriate handler
	handlers = {
		"batch": handle_batch_complete,
		"track": handle_tracking_update,
		"carrier_connected": handle_carrier_connected,
		"label_created": handle_label_created,
		"sales_order_status_change": handle_order_status_change,
	}

	handler = handlers.get(event_type)
	if handler:
		try:
			return handler(data)
		except Exception as e:
			frappe.log_error(
				title=f"ShipStation API Webhook Handler Error: {event_type}",
				message=str(e),
			)
			return {"status": "error", "message": str(e)}

	return {"status": "ignored", "message": f"Unknown event type: {event_type}"}


def handle_batch_complete(data: dict) -> dict:
	"""Handle batch label processing completion."""
	batch_id = data.get("data", {}).get("batch_id") or data.get("batch_id")

	if not batch_id:
		return {"status": "ignored", "message": "No batch_id in payload"}

	# Log batch completion
	frappe.logger("shipstation").debug(f"Batch {batch_id} processing complete")

	# Could trigger follow-up actions like downloading labels
	# For now, just acknowledge
	return {"status": "success", "message": f"Batch {batch_id} acknowledged"}


def handle_tracking_update(data: dict) -> dict:
	"""Handle tracking status updates."""
	tracking_data = data.get("data", {})
	tracking_number = tracking_data.get("tracking_number")
	status = tracking_data.get("status_description") or tracking_data.get("status")

	if not tracking_number:
		return {"status": "ignored", "message": "No tracking_number in payload"}

	# Update Shipment documents
	shipments = frappe.get_all(
		"Shipment",
		filters={"awb_number": tracking_number, "docstatus": 1},
		pluck="name",
	)

	for shipment_name in shipments:
		frappe.db.set_value(
			"Shipment",
			shipment_name,
			"tracking_status",
			status,
		)

	return {
		"status": "success",
		"message": f"Updated {len(shipments)} shipments",
	}


def handle_carrier_connected(data: dict) -> dict:
	"""Handle new carrier connection notification."""
	carrier_data = data.get("data", {})
	carrier_name = carrier_data.get("friendly_name") or carrier_data.get("carrier_code")

	frappe.logger("shipstation").info(f"New carrier connected: {carrier_name}")

	return {"status": "success", "message": f"Carrier {carrier_name} connection noted"}


def handle_label_created(data: dict) -> dict:
	"""Handle label creation notification."""
	label_data = data.get("data", {})
	label_id = label_data.get("label_id")

	return {"status": "success", "message": f"Label {label_id} noted"}


def handle_order_status_change(data: dict) -> dict:
	"""Handle order status change notification."""
	order_data = data.get("data", {})
	order_id = order_data.get("order_id")

	return {"status": "success", "message": f"Order {order_id} status update noted"}
