import json

import frappe
from frappe.utils import cstr, flt, get_datetime

from shipstation_integration.shipstation.shipments_adapter import (
	EXTERNAL_SHIPMENT_SOURCE,
	LABEL_SHIPMENT_SOURCE,
)

NO_DATE = "1900-01-01"


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
			"Sales Order", filters={**filters, "marketplace_order_id": ref}, pluck="name"
		)
		if names:
			return names
	if order_id:
		names = frappe.get_all(
			"Sales Order", filters={**filters, "shipstation_order_id": order_id}, pluck="name"
		)
		if names:
			return names
	return []


def upsert_exception(
	shipment_id,
	order_number,
	reason,
	matched_sales_orders,
	source,
	order_id=None,
	tracking_number=None,
):
	filters = {"status": "Open"}
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
		return

	name = frappe.db.get_value("ShipStation Shipment Exception", filters)
	if name:
		exc = frappe.get_doc("ShipStation Shipment Exception", name)
		if order_id:
			exc.order_id = order_id
	else:
		exc = frappe.new_doc("ShipStation Shipment Exception")
		exc.shipment_id = shipment_id
		exc.order_id = order_id
		exc.status = "Open"
	exc.order_number = order_number
	if tracking_number:
		exc.tracking_number = tracking_number
	exc.reason = reason
	exc.matched_sales_orders = matched_sales_orders
	exc.source = source
	exc.save(ignore_permissions=True)
	return exc


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
		so.remove(row)
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
	so.shipstation_shipment_status = target_status
	so.shipstation_tracking_number = target_tracking
	return True


def process_shipment(shipment, source, synchronous: bool = False):
	shipment_id = shipment.get("shipment_id")
	order_id = shipment.get("order_id")
	order_number = shipment.get("order_number")
	shipment_source = shipment.get("shipment_source") or (
		LABEL_SHIPMENT_SOURCE if shipment_id else EXTERNAL_SHIPMENT_SOURCE
	)
	is_external = shipment_source == EXTERNAL_SHIPMENT_SOURCE
	orders = find_sales_orders(order_number, order_id=order_id)

	if len(orders) != 1:
		upsert_exception(
			shipment_id,
			order_number,
			"Multiple Match" if orders else "No Match",
			",".join(orders),
			source,
			order_id=order_id,
		)
		return

	so = frappe.get_doc("Sales Order", orders[0])

	if is_external and has_active_label(so, order_id):
		return

	row = _find_shipment_row(so, shipment_id, order_id, is_external)
	is_new_row = row is None
	if is_new_row:
		row = so.append("shipments", {})
		if shipment_id:
			row.shipment_id = shipment_id

	new_status = (
		"Cancelled" if shipment.get("voided") or row.shipment_status == "Cancelled" else "Shipped"
	)
	new_tracking = shipment.get("tracking_number")
	new_carrier = shipment.get("carrier_code")
	new_method = shipment.get("service_code")
	new_date = shipment.get("ship_date")
	new_cost = shipment.get("shipment_cost")
	new_label = shipment.get("label_id")
	incoming_items = shipment.get("items")
	incoming_items_json = json.dumps(incoming_items)

	row_changed = (
		is_new_row
		or cstr(row.shipment_status) != cstr(new_status)
		or cstr(row.tracking_number) != cstr(new_tracking)
		or cstr(row.carrier) != cstr(new_carrier)
		or cstr(row.shipping_method) != cstr(new_method)
		or (new_date and get_datetime(row.shipment_date) != get_datetime(new_date))
		or flt(row.shipping_cost) != flt(new_cost)
		or cstr(row.label_reference) != cstr(new_label)
		or cstr(row.items_shipped) != incoming_items_json
		or cstr(row.order_id) != cstr(order_id)
		or cstr(row.shipment_source) != cstr(shipment_source)
	)

	row.shipment_status = new_status
	row.tracking_number = new_tracking
	row.carrier = new_carrier
	row.shipping_method = new_method
	if new_date:
		row.shipment_date = new_date
	row.shipping_cost = new_cost
	row.label_reference = new_label
	row.items_shipped = incoming_items_json
	row.shipment_source = shipment_source
	if order_id:
		row.order_id = order_id

	removed = remove_external_rows(so, order_id) if shipment_id else 0

	header_changed = sync_sales_order_header(so)

	if row_changed or header_changed or removed:
		for attempt in range(3):
			try:
				so.save(ignore_permissions=True)
				break
			except frappe.TimestampMismatchError:
				if not synchronous:
					raise frappe.RetryBackgroundJobError
				if attempt == 2:
					raise
				so.reload()
				row = _find_shipment_row(so, shipment_id, order_id, is_external)
				is_new_row = row is None
				if is_new_row:
					row = so.append("shipments", {})
					if shipment_id:
						row.shipment_id = shipment_id
				row.shipment_status = new_status
				row.tracking_number = new_tracking
				row.carrier = new_carrier
				row.shipping_method = new_method
				if new_date:
					row.shipment_date = new_date
				row.shipping_cost = new_cost
				row.label_reference = new_label
				row.items_shipped = incoming_items_json
				row.shipment_source = shipment_source
				if order_id:
					row.order_id = order_id
				if shipment_id:
					remove_external_rows(so, order_id)
				sync_sales_order_header(so)

	or_filters = []
	if shipment_id:
		or_filters.append({"shipment_id": shipment_id})
	if order_id:
		or_filters.append({"order_id": order_id})
	if or_filters:
		for name in frappe.get_all(
			"ShipStation Shipment Exception",
			filters={"status": "Open"},
			or_filters=or_filters,
			pluck="name",
		):
			frappe.db.set_value("ShipStation Shipment Exception", name, "status", "Resolved")
	# Auto-create Delivery Note per SDD Section 9
	if shipment.get("voided"):
		if shipment_id:
			existing_dn = frappe.db.get_value(
				"Delivery Note",
				{"shipstation_shipment_id": str(shipment_id), "docstatus": 1},
				"name",
			)
			if existing_dn:
				frappe.get_doc("Delivery Note", existing_dn).cancel()
	else:
		create_delivery_note_for_shipment(so, shipment)


def create_delivery_note_for_shipment(so, shipment) -> str | None:
	shipment_id = shipment.get("shipment_id")
	if not shipment_id:
		return None

	# Idempotency check: don't create duplicate Delivery Note for same shipment_id
	existing_dn = frappe.db.get_value(
		"Delivery Note",
		{"shipstation_shipment_id": str(shipment_id), "docstatus": ["!=", 2]},
		"name",
	)
	if existing_dn:
		return existing_dn

	if so.docstatus != 1 or flt(so.per_delivered) >= 100:
		return None

	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	try:
		dn = make_delivery_note(so.name)
		dn.shipstation_shipment_id = str(shipment_id)
		if shipment.get("order_id"):
			dn.shipstation_order_id = str(shipment.get("order_id"))
		if shipment.get("tracking_number"):
			dn.tracking_number = shipment.get("tracking_number")

		# Direct Sales partial shipment handling (eBay orders fulfilled in full)
		shipment_items = shipment.get("items")
		if shipment_items and not so.get("custom_ebay_order_reference"):
			shipped_map = {it["item_code"]: flt(it["quantity"]) for it in shipment_items if it.get("item_code")}
			if shipped_map:
				filtered_items = []
				for row in dn.items:
					if row.item_code in shipped_map:
						row.qty = min(row.qty, shipped_map[row.item_code])
						filtered_items.append(row)
				if filtered_items:
					dn.items = filtered_items

		dn.insert(ignore_permissions=True)
		dn.submit()
		return dn.name
	except Exception as e:
		frappe.log_error(
			title=f"Error auto-creating Delivery Note for SO {so.name}",
			message=frappe.get_traceback(),
		)
		raise e
