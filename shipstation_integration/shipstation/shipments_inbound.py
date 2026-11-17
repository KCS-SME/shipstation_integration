import json
import frappe
from frappe import _
from frappe.model.base_document import get_controller
from frappe.utils import cstr, flt, get_datetime, getdate

from shipstation_integration.shipstation.shipments_adapter import (
	EXTERNAL_SHIPMENT_SOURCE,
	LABEL_SHIPMENT_SOURCE,
)

NO_DATE = "1900-01-01"
BLOCKING_EXCEPTION_REASONS = [
	"Insufficient Stock",
	"DN Creation Failed",
	"No Item Details",
	"eBay Short Pick",
	"Sales Order Not Deliverable",
	"Delivery Note Cancelled",
]


def _warm_metadata():
	doctypes = [
		"Sales Order",
		"Sales Order Item",
		"Sales Taxes and Charges",
		"Item Wise Tax Detail",
		"Packed Item",
		"Pricing Rule Detail",
		"Payment Schedule",
		"Sales Team",
		"Sales Order Shipment",
		"Delivery Note",
		"Delivery Note Item",
		"Bin",
		"Pick List",
		"Pick List Item",
		"Item",
	]
	for dt in doctypes:
		frappe.get_meta(dt)
		get_controller(dt)


def find_sales_orders(order_number, order_id=None):
	filters = {"docstatus": ["!=", 2]}
	if order_number:
		ref = str(order_number).strip()
		names = frappe.get_all("Sales Order", filters={**filters, "name": ref}, pluck="name")
		if names:
			return names
		names = frappe.get_all(
			"Sales Order", filters={**filters, "custom_ebay_order_reference": ref}, pluck="name"
		)
		if names:
			return names
		names = frappe.get_all(
			"Sales Order", filters={**filters, "custom_shopify_order_reference": ref}, pluck="name"
		)
		if names:
			return names
		names = frappe.get_all("Sales Order", filters={**filters, "po_no": ref}, pluck="name")
		if names:
			return names
	if order_id:
		ref_id = str(order_id).strip()
		names = frappe.get_all(
			"Sales Order", filters={**filters, "custom_ebay_order_reference": ref_id}, pluck="name"
		)
		if names:
			return names
		names = frappe.get_all(
			"Sales Order", filters={**filters, "custom_shopify_order_reference": ref_id}, pluck="name"
		)
		if names:
			return names
	return []


def _is_repair_order(order_number, order_id=None):
	ref = str(order_number or "").strip()
	if not ref:
		return False
	prefixes = []
	settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
	if settings_names:
		prefix_val = frappe.db.get_value("Shipstation Settings", settings_names[0], "repair_order_prefix")
		if prefix_val:
			prefixes = [p.strip() for p in prefix_val.split(",") if p.strip()]
	return any(ref.startswith(p) for p in prefixes) if prefixes else False


def _bump_parent_modified(so):
	now_ts = frappe.utils.now()
	so.db_set("modified", now_ts, update_modified=False)
	so.modified = now_ts


def upsert_exception(
	shipment_id,
	order_number,
	reason,
	matched_sales_orders,
	source,
	order_id=None,
	tracking_number=None,
	sales_order=None,
	error_message=None,
	payload=None,
	status="Open",
):
	filters = {}
	if status == "Ignored":
		filters["status"] = "Ignored"
	else:
		filters["status"] = "Open"
	if source == "Outbound":
		filters["source"] = "Outbound"
		filters["reason"] = reason
		if shipment_id:
			filters["shipment_id"] = shipment_id
		elif order_id:
			filters["order_id"] = order_id
		else:
			filters["order_number"] = order_number
	elif shipment_id:
		filters["shipment_id"] = shipment_id
	elif order_id:
		filters["order_id"] = order_id
	else:
		frappe.log_error(
			title="ShipStation shipment exception without identifier",
			message=(
				f"An exception for order {order_number} could not be recorded because neither a "
				"shipment id nor an order id is known."
			),
		)
		return None

	name = frappe.db.get_value("ShipStation Shipment Exception", filters)
	is_new = name is None
	prev_reason = None
	if name:
		exc = frappe.get_doc("ShipStation Shipment Exception", name)
		prev_reason = exc.reason
		if order_id:
			exc.order_id = order_id
	else:
		exc = frappe.new_doc("ShipStation Shipment Exception")
		exc.shipment_id = shipment_id or (f"EXT-{order_id}" if order_id else None)
		exc.order_id = order_id
	exc.status = status
	exc.order_number = order_number
	if tracking_number:
		exc.tracking_number = tracking_number
	exc.reason = reason
	exc.matched_sales_orders = matched_sales_orders
	exc.source = source
	if sales_order:
		exc.sales_order = sales_order
	if error_message:
		exc.error_message = error_message
	if payload:
		exc.payload = json.dumps(payload) if isinstance(payload, dict | list) else str(payload)
	exc.save(ignore_permissions=True)

	should_notify = (
		status == "Open"
		and reason == "Insufficient Stock"
		and (is_new or prev_reason != "Insufficient Stock" or source == "Retry")
	)
	if should_notify:
		_enqueue_insufficient_stock_notification(exc)
	return exc


def _enqueue_insufficient_stock_notification(exc):
	notification_name = "Insufficient Stock Shipment Exception"
	if not frappe.db.exists("Notification", notification_name):
		return
	notif = frappe.get_doc("Notification", notification_name)
	if not notif.enabled:
		return
	settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
	role = "Stock Manager"
	if settings_names:
		settings_role = frappe.db.get_value("Shipstation Settings", settings_names[0], "notification_role")
		if settings_role:
			role = settings_role
	if not notif.recipients:
		notif.append("recipients", {"receiver_by_role": role})
	else:
		notif.recipients[0].receiver_by_role = role
	frappe.flags.last_insufficient_stock_notification_sent = exc.name
	frappe.enqueue(
		"frappe.email.doctype.notification.notification.evaluate_alert",
		doc=exc,
		alert=notif,
		now=frappe.flags.in_test or bool(not getattr(frappe.local, "request", None)),
		enqueue_after_commit=False,
	)


def _auto_resolve_blocking_exceptions(shipment_id, order_id=None, source=None):
	filters = {"status": "Open", "reason": ["in", BLOCKING_EXCEPTION_REASONS]}
	if shipment_id:
		filters["shipment_id"] = str(shipment_id)
	elif order_id:
		filters["order_id"] = str(order_id)
	else:
		return
	for exc_name in frappe.get_all(
		"ShipStation Shipment Exception",
		filters=filters,
		pluck="name",
	):
		frappe.db.set_value("ShipStation Shipment Exception", exc_name, "status", "Resolved")


def _record_retry_replacement(shipment_id, order_id, new_dn, cancelled_dn):
	filters = {}
	if shipment_id:
		filters["shipment_id"] = str(shipment_id)
	elif order_id:
		filters["order_id"] = str(order_id)
	if filters:
		names = frappe.get_all("ShipStation Shipment Exception", filters=filters, pluck="name", order_by="modified desc")
		if names:
			msg = _("Replacement Delivery Note {0} created on retry after previous DN {1} was cancelled.").format(new_dn, cancelled_dn)
			frappe.db.set_value("ShipStation Shipment Exception", names[0], "error_message", msg, update_modified=False)


def delivery_note_on_cancel(doc, method=None):
	if not getattr(doc, "shipstation_shipment_id", None):
		return
	ignored = getattr(doc, "ignore_linked_doctypes", ()) or ()
	if isinstance(ignored, str):
		ignored = (ignored,)
	if "Sales Order" not in ignored:
		doc.ignore_linked_doctypes = tuple(ignored) + ("Sales Order",)

	eff_id = str(doc.shipstation_shipment_id)
	order_id = getattr(doc, "shipstation_order_id", None)
	if not order_id and eff_id.startswith("EXT-"):
		order_id = eff_id[4:]
	shipment_id = None if eff_id.startswith("EXT-") else eff_id

	so_names = {item.against_sales_order for item in doc.items if item.against_sales_order}
	for so_name in so_names:
		so = frappe.get_doc("Sales Order", so_name)
		for r in so.shipments:
			if (shipment_id and r.shipment_id == shipment_id) or (r.delivery_note == doc.name) or (order_id and r.order_id == order_id):
				r.db_set({"flagged_for_check": 1, "review_reason": "Delivery Note Cancelled"}, update_modified=False)
				_bump_parent_modified(so)
				break

	tracking = getattr(doc, "tracking_number", None)
	upsert_exception(
		shipment_id,
		next(iter(so_names), None),
		"Delivery Note Cancelled",
		", ".join(sorted(so_names)),
		"Webhook",
		order_id=order_id,
		tracking_number=tracking,
		sales_order=next(iter(so_names), None),
		error_message=doc.name,
	)


def _find_shipment_row(so, shipment_id, order_id, is_external):
	for row in so.shipments:
		if is_external:
			if not row.shipment_id and order_id and row.order_id == order_id:
				return row
		elif row.shipment_id == shipment_id:
			return row
		elif not row.shipment_id and not shipment_id and order_id and row.order_id == order_id:
			return row
	return None


def has_active_label(so, order_id=None):
	for row in so.shipments:
		if not row.shipment_id or row.shipment_status == "Cancelled":
			continue
		if order_id and row.order_id and row.order_id != order_id:
			continue
		return True
	return False


def remove_external_rows(so, order_id):
	if not order_id:
		return 0
	removed = 0
	for row in list(so.shipments):
		if row.shipment_id or row.order_id != order_id:
			continue
		if row.delivery_note or row.outcome == "Delivery Note Created" or row.replaced_by_shipment_id:
			if row.shipment_status != "Cancelled":
				row.db_set("shipment_status", "Cancelled", update_modified=False)
				row.shipment_status = "Cancelled"
				_bump_parent_modified(so)
		else:
			frappe.db.delete("Sales Order Shipment", {"name": row.name})
			so.shipments.remove(row)
			_bump_parent_modified(so)
			removed += 1
	return removed


def _header_values(so):
	active = [r for r in so.shipments if r.shipment_status != "Cancelled"]
	latest = (
		sorted(active, key=lambda r: get_datetime(r.shipment_date or NO_DATE))[-1] if active else None
	)
	target_status = latest.shipment_status if latest else None
	target_tracking = latest.tracking_number if latest else None
	return target_status, target_tracking


def sync_sales_order_header(so) -> bool:
	target_status, target_tracking = _header_values(so)
	if (
		cstr(so.shipstation_shipment_status) == cstr(target_status)
		and cstr(so.shipstation_tracking_number) == cstr(target_tracking)
	):
		return False
	so.db_set({
		"shipstation_shipment_status": target_status,
		"shipstation_tracking_number": target_tracking,
	}, update_modified=False)
	_bump_parent_modified(so)
	return True


def _get_item_qty_map(items):
	result = {}
	for it in items or []:
		code = it.get("item_code")
		if code:
			result[code] = result.get(code, 0.0) + flt(it.get("quantity"))
	return result


def process_shipment(shipment, source, synchronous: bool = False):
	shipment_id = shipment.get("shipment_id")
	order_id = shipment.get("order_id")
	order_number = shipment.get("order_number")
	shipment_date = shipment.get("ship_date")
	shipment_source = shipment.get("shipment_source") or (
		LABEL_SHIPMENT_SOURCE if shipment_id else EXTERNAL_SHIPMENT_SOURCE
	)
	is_external = shipment_source == EXTERNAL_SHIPMENT_SOURCE
	effective_shipment_id = str(shipment_id) if shipment_id else (f"EXT-{order_id}" if order_id else None)

	if source != "Retry" and shipment_date:
		settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
		if settings_names:
			since_date = frappe.db.get_value("Shipstation Settings", settings_names[0], "since_date")
			if since_date and getdate(shipment_date) < getdate(since_date):
				return

	orders = find_sales_orders(order_number, order_id=order_id)
	if len(orders) != 1:
		if not orders:
			if _is_repair_order(order_number, order_id):
				upsert_exception(
					shipment_id,
					order_number,
					"Repair Shipment",
					"",
					source,
					order_id=order_id,
					payload=shipment,
					status="Ignored",
					error_message=_("Repair order shipment ignored based on prefix rule."),
				)
				frappe.db.commit()
				return
			if shipment.get("voided"):
				return
			err_msg = _(
				"The shipment did not match exactly one Sales Order. "
				"Combined eBay orders in ShipStation lose their original eBay Order ID and cannot be matched."
			)
			upsert_exception(
				shipment_id,
				order_number,
				"No Match",
				"",
				source,
				order_id=order_id,
				payload=shipment,
				error_message=err_msg,
			)
			frappe.db.commit()
			return
		else:
			err_msg = _("The shipment matched multiple Sales Orders: {0}").format(", ".join(orders))
			upsert_exception(
				shipment_id,
				order_number,
				"Multiple Match",
				",".join(orders),
				source,
				order_id=order_id,
				payload=shipment,
				error_message=err_msg,
			)
			frappe.db.commit()
			return

	_warm_metadata()
	frappe.db.commit()

	so = frappe.get_doc("Sales Order", orders[0], for_update=True)

	row = _find_shipment_row(so, shipment_id, order_id, is_external)
	is_new_row = row is None

	new_status = "Cancelled" if shipment.get("voided") else "Shipped"
	new_tracking = shipment.get("tracking_number")
	new_carrier = shipment.get("carrier_code")
	new_method = shipment.get("service_code")
	new_cost = shipment.get("shipment_cost")
	new_label = shipment.get("label_id")
	incoming_items = shipment.get("items")
	incoming_items_json = json.dumps(incoming_items) if incoming_items else None

	if is_new_row:
		max_idx = max([r.idx for r in so.shipments], default=0)
		row = frappe.get_doc({
			"doctype": "Sales Order Shipment",
			"parent": so.name,
			"parenttype": "Sales Order",
			"parentfield": "shipments",
			"docstatus": 1,
			"idx": max_idx + 1,
			"shipment_id": shipment_id,
			"order_id": order_id,
			"shipment_status": new_status,
			"outcome": "Pending",
			"tracking_number": new_tracking,
			"carrier": new_carrier,
			"shipping_method": new_method,
			"shipment_date": shipment_date,
			"shipping_cost": new_cost,
			"label_reference": new_label,
			"items_shipped": incoming_items_json,
			"shipment_source": shipment_source,
		}).insert(ignore_permissions=True)
		so.shipments.append(row)
		_bump_parent_modified(so)
	else:
		row_updates = {
			"tracking_number": new_tracking,
			"carrier": new_carrier,
			"shipping_method": new_method,
			"label_reference": new_label,
			"shipment_source": shipment_source,
		}
		if new_cost is not None:
			row_updates["shipping_cost"] = flt(new_cost)
		if incoming_items:
			row_updates["items_shipped"] = incoming_items_json
		if new_status == "Cancelled":
			row_updates["shipment_status"] = "Cancelled"
		if shipment_date:
			row_updates["shipment_date"] = shipment_date
		if order_id:
			row_updates["order_id"] = order_id
		row.db_set(row_updates, update_modified=False)
		_bump_parent_modified(so)

	if shipment_id:
		remove_external_rows(so, order_id)
	sync_sales_order_header(so)

	if shipment.get("voided"):
		row.db_set({"shipment_status": "Cancelled", "outcome": "Tracking Only"}, update_modified=False)
		_bump_parent_modified(so)
		_auto_resolve_blocking_exceptions(shipment_id, order_id, source=source)
		frappe.db.commit()
		return

	if row.outcome in ("Delivery Note Created", "Tracking Only"):
		if source == "Retry" and row.delivery_note:
			dn_status = frappe.db.get_value("Delivery Note", row.delivery_note, "docstatus")
			if dn_status == 2:
				pass
			else:
				frappe.db.commit()
				return
		else:
			if not row.delivery_note and effective_shipment_id:
				existing_dn = frappe.db.get_value(
					"Delivery Note",
					{"shipstation_shipment_id": effective_shipment_id},
					"name",
				)
				if existing_dn:
					row.db_set("delivery_note", existing_dn, update_modified=False)
					_bump_parent_modified(so)
			frappe.db.commit()
			return

	if so.docstatus != 1 or so.status in ("Closed", "On Hold", "Cancelled"):
		row.db_set("outcome", "Failed", update_modified=False)
		_bump_parent_modified(so)
		upsert_exception(
			shipment_id,
			order_number,
			"Sales Order Not Deliverable",
			so.name,
			source,
			order_id=order_id,
			sales_order=so.name,
			payload=shipment,
		)
		frappe.db.commit()
		return

	if is_external and (has_active_label(so, order_id) or any(r.shipment_id for r in so.shipments)):
		row.db_set("outcome", "Tracking Only", update_modified=False)
		_bump_parent_modified(so)
		frappe.db.commit()
		return

	has_invalid_items = (
		not incoming_items
		or any(not it.get("item_code") or flt(it.get("quantity")) <= 0 for it in incoming_items)
	)
	if has_invalid_items:
		row.db_set("outcome", "Failed", update_modified=False)
		_bump_parent_modified(so)
		upsert_exception(
			shipment_id,
			order_number,
			"No Item Details",
			so.name,
			source,
			order_id=order_id,
			sales_order=so.name,
			payload=shipment,
		)
		frappe.db.commit()
		return

	is_ebay = bool((so.get("custom_ebay_order_reference") or "").strip())

	if is_ebay:
		earlier_dn_row = next((r for r in so.shipments if r.delivery_note and r.name != row.name), None)
		if earlier_dn_row:
			row.db_set("outcome", "Tracking Only", update_modified=False)
			_bump_parent_modified(so)
			if earlier_dn_row.shipment_id:
				try:
					settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
					if settings_names:
						sss_doc = frappe.get_doc("Shipstation Settings", settings_names[0])
						client = sss_doc.client()
						order_obj = client.get_order(order_id)
						store_id = getattr(getattr(order_obj, "advanced_options", None), "store_id", None)
						if store_id:
							from shipstation_integration.shipstation.shipments_poller import fetch_shipments_of_order
							shipments_list = fetch_shipments_of_order(client, str(store_id), str(order_id))
							for s in shipments_list:
								if str(s.get("shipmentId")) == str(earlier_dn_row.shipment_id) and s.get("voided"):
									row.db_set({"flagged_for_check": 1, "review_reason": "Replacement Label Review"}, update_modified=False)
									_bump_parent_modified(so)
									upsert_exception(
										shipment_id,
										order_number,
										"Replacement Label Review",
										so.name,
										source,
										order_id=order_id,
										sales_order=so.name,
										payload=shipment,
									)
									break
				except Exception:
					pass
			frappe.db.commit()
			return
	else:
		incoming_items_map = _get_item_qty_map(incoming_items)
		candidates = []
		if incoming_items_map:
			for r in so.shipments:
				if not r.delivery_note or r.replaced_by_shipment_id or r.name == row.name:
					continue
				cand_items = json.loads(r.items_shipped) if r.items_shipped else []
				cand_map = _get_item_qty_map(cand_items)
				if cand_map and cand_map == incoming_items_map:
					candidates.append(r)

		candidate = None
		if candidates:
			candidates.sort(key=lambda c: (0 if c.shipment_status == "Cancelled" else 1, -c.idx))
			candidate = candidates[0]

		if candidate:
			if candidate.shipment_status == "Cancelled":
				candidate.db_set("replaced_by_shipment_id", shipment_id or f"EXT-{order_id}", update_modified=False)
				row.db_set({"outcome": "Tracking Only", "flagged_for_check": 1, "review_reason": "Replacement Label Review"}, update_modified=False)
				_bump_parent_modified(so)
				upsert_exception(
					shipment_id,
					order_number,
					"Replacement Label Review",
					so.name,
					source,
					order_id=order_id,
					sales_order=so.name,
					payload=shipment,
				)
				frappe.db.commit()
				return
			else:
				cand_voided_state = None
				try:
					settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
					if settings_names:
						sss_doc = frappe.get_doc("Shipstation Settings", settings_names[0])
						client = sss_doc.client()
						order_obj = client.get_order(order_id)
						store_id = getattr(getattr(order_obj, "advanced_options", None), "store_id", None)
						if store_id:
							from shipstation_integration.shipstation.shipments_poller import fetch_shipments_of_order
							shipments_list = fetch_shipments_of_order(client, str(store_id), str(order_id))
							for s in shipments_list:
								if str(s.get("shipmentId")) == str(candidate.shipment_id):
									cand_voided_state = bool(s.get("voided"))
									break
				except Exception:
					cand_voided_state = None

				if cand_voided_state is None:
					row.db_set("outcome", "Failed", update_modified=False)
					_bump_parent_modified(so)
					upsert_exception(
						shipment_id,
						order_number,
						"Replacement Label Review",
						so.name,
						source,
						order_id=order_id,
						sales_order=so.name,
						payload=shipment,
					)
					frappe.db.commit()
					return
				elif cand_voided_state is True:
					candidate.db_set("replaced_by_shipment_id", shipment_id or f"EXT-{order_id}", update_modified=False)
					row.db_set({"outcome": "Tracking Only", "flagged_for_check": 1, "review_reason": "Replacement Label Review"}, update_modified=False)
					_bump_parent_modified(so)
					upsert_exception(
						shipment_id,
						order_number,
						"Replacement Label Review",
						so.name,
						source,
						order_id=order_id,
						sales_order=so.name,
						payload=shipment,
					)
					frappe.db.commit()
					return

	if flt(so.per_delivered) >= 100 and not any(r.delivery_note for r in so.shipments if r.name != row.name):
		row.db_set("outcome", "Failed", update_modified=False)
		_bump_parent_modified(so)
		upsert_exception(
			shipment_id,
			order_number,
			"Sales Order Not Deliverable",
			so.name,
			source,
			order_id=order_id,
			sales_order=so.name,
			payload=shipment,
		)
		frappe.db.commit()
		return

	shipped_map = _get_item_qty_map(incoming_items)
	allocated_by_so_detail = {}
	has_excess = False

	if is_ebay:
		so_ordered_sum = {}
		for it in so.items:
			so_ordered_sum[it.item_code] = so_ordered_sum.get(it.item_code, 0.0) + flt(it.qty)

		for code, total_ordered in so_ordered_sum.items():
			if shipped_map.get(code, 0.0) < total_ordered:
				row.db_set("outcome", "Failed", update_modified=False)
				_bump_parent_modified(so)
				upsert_exception(
					shipment_id,
					order_number,
					"eBay Short Pick",
					so.name,
					source,
					order_id=order_id,
					sales_order=so.name,
					payload=shipment,
				)
				frappe.db.commit()
				return

		rem_shipped = dict(shipped_map)
		for it in so.items:
			pending = max(flt(it.qty) - flt(it.delivered_qty), 0.0)
			avail = rem_shipped.get(it.item_code, 0.0)
			alloc = min(avail, pending)
			if alloc > 0:
				allocated_by_so_detail[it.name] = alloc
				rem_shipped[it.item_code] -= alloc
	else:
		total_pending = sum(max(flt(it.qty) - flt(it.delivered_qty), 0.0) for it in so.items)
		if total_pending <= 0:
			row.db_set("outcome", "Failed", update_modified=False)
			_bump_parent_modified(so)
			upsert_exception(
				shipment_id,
				order_number,
				"Sales Order Not Deliverable",
				so.name,
				source,
				order_id=order_id,
				sales_order=so.name,
				payload=shipment,
			)
			frappe.db.commit()
			return

		pending_by_code = {}
		for it in so.items:
			pending_by_code[it.item_code] = pending_by_code.get(it.item_code, 0.0) + max(
				flt(it.qty) - flt(it.delivered_qty), 0.0
			)

		for code, shipped_qty in shipped_map.items():
			if shipped_qty > pending_by_code.get(code, 0.0):
				has_excess = True

		rem_shipped = dict(shipped_map)
		for it in so.items:
			pending = max(flt(it.qty) - flt(it.delivered_qty), 0.0)
			avail = rem_shipped.get(it.item_code, 0.0)
			alloc = min(avail, pending)
			if alloc > 0:
				allocated_by_so_detail[it.name] = alloc
				rem_shipped[it.item_code] -= alloc

		if not allocated_by_so_detail:
			row.db_set("outcome", "Failed", update_modified=False)
			_bump_parent_modified(so)
			upsert_exception(
				shipment_id,
				order_number,
				"Sales Order Not Deliverable",
				so.name,
				source,
				order_id=order_id,
				sales_order=so.name,
				payload=shipment,
			)
			frappe.db.commit()
			return

	pick_list_links = {}
	has_draft_pl = False
	has_serial_review = False
	linked_pl_headers = set()

	for so_item in so.items:
		if so_item.name not in allocated_by_so_detail:
			continue
		alloc_qty = allocated_by_so_detail[so_item.name]

		submitted_plis = frappe.get_all(
			"Pick List Item",
			filters={"sales_order_item": so_item.name, "docstatus": 1},
			fields=["name", "picked_qty", "delivered_qty", "parent"],
			order_by="creation asc",
		)
		eligible_pli = next(
			(pli for pli in submitted_plis if flt(pli.picked_qty) - flt(pli.delivered_qty) > 0),
			None,
		)
		if eligible_pli:
			rem_picked = flt(eligible_pli.picked_qty) - flt(eligible_pli.delivered_qty)
			if rem_picked >= alloc_qty:
				pick_list_links[so_item.name] = (eligible_pli.parent, eligible_pli.name)
				linked_pl_headers.add(eligible_pli.parent)
				if rem_picked != alloc_qty:
					has_serial_review = True
			else:
				has_serial_review = True
		else:
			draft_pli = frappe.db.get_value(
				"Pick List Item",
				{"sales_order_item": so_item.name, "docstatus": 0},
				"name",
			)
			if draft_pli:
				has_draft_pl = True

	if has_draft_pl:
		row.db_set({"flagged_for_check": 1, "review_reason": "Pick List Pending"}, update_modified=False)
		_bump_parent_modified(so)
	elif has_serial_review:
		row.db_set({"flagged_for_check": 1, "review_reason": "Serial Review"}, update_modified=False)
		_bump_parent_modified(so)

	cancelled_dn = None
	if effective_shipment_id:
		existing_dns = frappe.get_all(
			"Delivery Note",
			filters={"shipstation_shipment_id": effective_shipment_id},
			fields=["name", "docstatus"],
			order_by="creation desc",
		)
		active_dn = next((d.name for d in existing_dns if d.docstatus != 2), None)
		cancelled_dn = next((d.name for d in existing_dns if d.docstatus == 2), None)
		if active_dn:
			update_dict = {"outcome": "Delivery Note Created", "delivery_note": active_dn}
			if row.review_reason == "Delivery Note Cancelled":
				update_dict["flagged_for_check"] = 0
				update_dict["review_reason"] = None
			row.db_set(update_dict, update_modified=False)
			_bump_parent_modified(so)
			_auto_resolve_blocking_exceptions(shipment_id, order_id, source=source)
			frappe.db.commit()
			return
		elif cancelled_dn and source != "Retry":
			row.db_set({"outcome": "Delivery Note Created", "delivery_note": cancelled_dn}, update_modified=False)
			_bump_parent_modified(so)
			frappe.db.commit()
			return

	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	frappe.db.savepoint("create_delivery_note")
	try:
		dn = make_delivery_note(so.name)
		dn.shipstation_shipment_id = effective_shipment_id
		if order_id:
			dn.shipstation_order_id = str(order_id)
		if new_tracking:
			dn.tracking_number = new_tracking
		if linked_pl_headers and dn.meta.has_field("pick_list"):
			dn.pick_list = next(iter(linked_pl_headers))

		filtered_items = []
		for dn_item in dn.items:
			if dn_item.so_detail in allocated_by_so_detail:
				dn_item.qty = allocated_by_so_detail[dn_item.so_detail]
				dn_item.stock_qty = flt(dn_item.qty) * flt(dn_item.conversion_factor or 1.0)
				if dn_item.against_sales_order and dn_item.so_detail in pick_list_links:
					pl_name, pli_name = pick_list_links[dn_item.so_detail]
					dn_item.against_pick_list = pl_name
					dn_item.pick_list_item = pli_name
				filtered_items.append(dn_item)

		if not filtered_items:
			frappe.throw(_("No deliverable items matched for Delivery Note creation"))

		dn.items = filtered_items
		dn.set_missing_values()
		dn.calculate_taxes_and_totals()

		if not is_ebay and flt(so.net_total) > 0 and dn.taxes:
			remaining_so_pending = sum(
				max(flt(it.qty) - flt(it.delivered_qty) - allocated_by_so_detail.get(it.name, 0.0), 0.0)
				for it in so.items
			)
			is_remainder_dn = remaining_so_pending <= 0.0001
			for dn_tax in dn.taxes:
				if dn_tax.charge_type != "Actual":
					continue
				so_tax = next((t for t in so.taxes if t.account_head == dn_tax.account_head and t.charge_type == "Actual"), None)
				if not so_tax:
					continue
				so_actual = flt(so_tax.tax_amount)
				prec = dn.precision("tax_amount", dn_tax) or 2
				if is_remainder_dn:
					prev_dn_names = frappe.get_all(
						"Delivery Note Item",
						filters={"against_sales_order": so.name, "docstatus": 1},
						pluck="parent",
						distinct=True,
					)
					already_allocated = 0.0
					if prev_dn_names:
						for prev_tax in frappe.get_all(
							"Sales Taxes and Charges",
							filters={"parent": ["in", prev_dn_names], "account_head": dn_tax.account_head, "charge_type": "Actual"},
							fields=["tax_amount"],
						):
							already_allocated += flt(prev_tax.tax_amount)
					dn_tax.tax_amount = max(0.0, flt(so_actual - already_allocated, prec))
				else:
					dn_tax.tax_amount = flt(so_actual * flt(dn.net_total) / flt(so.net_total), prec)
			dn.calculate_taxes_and_totals()

		dn.flags.ignore_permissions = True
		dn.insert()
		dn.submit()

		frappe.db.release_savepoint("create_delivery_note")
		update_dict = {"outcome": "Delivery Note Created", "delivery_note": dn.name}
		if row.review_reason == "Delivery Note Cancelled":
			update_dict["flagged_for_check"] = 0
			update_dict["review_reason"] = None
		row.db_set(update_dict, update_modified=False)
		_bump_parent_modified(so)
		_auto_resolve_blocking_exceptions(shipment_id, order_id, source=source)
		if source == "Retry" and cancelled_dn:
			_record_retry_replacement(shipment_id, order_id, dn.name, cancelled_dn)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback(save_point="create_delivery_note")
		is_deadlock_or_timeout = isinstance(e, (frappe.QueryDeadlockError, frappe.QueryTimeoutError))
		if not is_deadlock_or_timeout and hasattr(e, "args") and len(e.args) > 0 and e.args[0] in (1205, 1213):
			is_deadlock_or_timeout = True
		if is_deadlock_or_timeout:
			raise e

		frappe.log_error(title=f"Delivery Note Creation Failed: {so.name}", message=frappe.get_traceback())

		from erpnext.stock.stock_ledger import NegativeStockError
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import BatchNegativeStockError
		from erpnext.stock.doctype.stock_ledger_entry.stock_ledger_entry import InventoryDimensionNegativeStockError

		if isinstance(e, (NegativeStockError, BatchNegativeStockError, InventoryDimensionNegativeStockError)):
			reason = "Insufficient Stock"
		else:
			reason = "DN Creation Failed"

		row.db_set("outcome", "Failed", update_modified=False)
		_bump_parent_modified(so)
		upsert_exception(
			shipment_id,
			order_number,
			reason,
			so.name,
			source,
			order_id=order_id,
			tracking_number=new_tracking,
			sales_order=so.name,
			error_message=str(e),
			payload=shipment,
		)
		frappe.db.commit()
		return

	if has_excess:
		upsert_exception(
			shipment_id,
			order_number,
			"Quantity Exceeds Pending",
			so.name,
			source,
			order_id=order_id,
			sales_order=so.name,
			payload=shipment,
		)
		frappe.db.commit()

	if has_draft_pl:
		upsert_exception(
			shipment_id,
			order_number,
			"Pick List Pending",
			so.name,
			source,
			order_id=order_id,
			sales_order=so.name,
			payload=shipment,
		)
		frappe.db.commit()
