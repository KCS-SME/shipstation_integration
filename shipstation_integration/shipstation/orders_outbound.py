import json
import re

import httpx
import frappe
from frappe import _
from frappe.utils import cint, flt

from shipstation_integration.shipstation.shipments_inbound import has_active_label, upsert_exception


def get_active_settings(throw_if_missing: bool = True):
	names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
	if not names:
		if throw_if_missing:
			frappe.throw(_("No enabled Shipstation Settings record found"))
		return None
	return frappe.get_doc("Shipstation Settings", names[0])


def get_so_channel(so) -> str:
	if isinstance(so, str):
		so = frappe.get_doc("Sales Order", so)

	ebay_ref = (so.get("custom_ebay_order_reference") or "").strip()
	shopify_ref = (so.get("custom_shopify_order_reference") or "").strip()

	if ebay_ref and shopify_ref:
		frappe.throw(
			_("Sales Order {0} has conflicting channel references (eBay: {1}, Shopify: {2})").format(
				so.name, ebay_ref, shopify_ref
			)
		)

	if ebay_ref:
		return "eBay"
	if shopify_ref:
		return "Shopify"
	return "Direct Sales"


def get_channel_config(channel_name: str, settings=None) -> dict:
	if not settings:
		settings = get_active_settings(throw_if_missing=False)

	if not settings:
		return {
			"channel": channel_name,
			"send_to_shipstation": False,
			"store_id": "",
		}

	for row in getattr(settings, "channels", []):
		if row.channel == channel_name:
			return {
				"channel": row.channel,
				"send_to_shipstation": bool(row.send_to_shipstation),
				"store_id": (row.store_id or "").strip(),
			}

	return {
		"channel": channel_name,
		"send_to_shipstation": False,
		"store_id": "",
	}


def check_store_track_shipments(store_id: str, settings=None) -> bool:
	if not store_id:
		return False
	if not settings:
		settings = get_active_settings(throw_if_missing=False)

	if not settings:
		return False

	for store in getattr(settings, "shipstation_stores", []):
		if str(store.store_id) == str(store_id) and store.track_shipments:
			return True
	return False


def check_so_fulfillment_gate(so, settings=None) -> tuple[bool, str | None, bool]:
	if isinstance(so, str):
		so = frappe.get_doc("Sales Order", so)

	if so.docstatus != 1:
		return False, _("Sales Order {0} is not submitted").format(so.name), False

	if so.status in ["Closed", "Completed", "Cancelled"]:
		return False, _("Sales Order {0} status is {1}").format(so.name, so.status), False

	if flt(so.per_delivered) >= 100:
		return False, _("Sales Order {0} is already fully delivered").format(so.name), False

	channel = get_so_channel(so)
	if not settings:
		settings = get_active_settings(throw_if_missing=False)

	if not settings:
		return False, _("No enabled Shipstation Settings record found"), False

	cfg = get_channel_config(channel, settings=settings)
	if not cfg.get("send_to_shipstation"):
		return False, _("{0} channel is not enabled in Shipstation Settings").format(channel), False

	if not cfg.get("store_id"):
		return (
			False,
			_("{0} channel has no store_id configured in Shipstation Settings").format(channel),
			False,
		)

	is_linked = bool(so.shipstation_order_id)
	has_key = bool(so.shipstation_order_key)

	if not is_linked:
		return True, None, False

	if is_linked and not has_key:
		return (
			False,
			_("Sales Order {0} is a legacy inbound order without an order key").format(so.name),
			False,
		)

	if has_active_label(so):
		return (
			False,
			_("Sales Order {0} already has an active shipping label in ShipStation").format(so.name),
			False,
		)

	return True, None, True


def validate_so_fulfillment_gate(so, settings=None) -> bool:
	eligible, reason, _is_update = check_so_fulfillment_gate(so, settings=settings)
	if not eligible:
		frappe.throw(reason)
	return True


def resolve_country_iso(country_name: str) -> str | None:
	if not country_name:
		return None
	cleaned = country_name.strip()
	if len(cleaned) == 2 and cleaned.isalpha():
		return cleaned.upper()
	code = frappe.db.get_value("Country", cleaned, "code")
	if code and len(code.strip()) == 2:
		return code.strip().upper()
	return None


def build_address_dict(address_name: str, recipient_name: str, address_role: str, phone: str = "") -> dict:
	if not address_name:
		frappe.throw(_("{0} address is missing").format(address_role))

	if not (recipient_name or "").strip():
		frappe.throw(_("{0} name is missing").format(address_role))

	addr_doc = frappe.get_doc("Address", address_name)

	street1 = (addr_doc.address_line1 or "").strip()
	if not street1:
		frappe.throw(_("{0} Address Line 1 is missing").format(address_role))

	city = (addr_doc.city or "").strip()
	if not city:
		frappe.throw(_("{0} City is missing").format(address_role))

	postal_code = (addr_doc.pincode or "").strip()
	if not postal_code:
		frappe.throw(_("{0} Postal Code / PIN Code is missing").format(address_role))

	country_name = (addr_doc.country or "").strip()
	if not country_name:
		frappe.throw(_("{0} Country is missing").format(address_role))

	country_iso = resolve_country_iso(country_name)
	if not country_iso:
		frappe.throw(
			_("Could not resolve 2-letter ISO country code for country '{0}' on {1}").format(
				country_name, address_role
			)
		)

	result = {
		"name": recipient_name.strip(),
		"street1": street1,
		"city": city,
		"postalCode": postal_code,
		"country": country_iso,
	}

	street2 = (addr_doc.address_line2 or "").strip()
	if street2:
		result["street2"] = street2

	state = (addr_doc.state or "").strip()
	if state:
		result["state"] = state

	phone_val = (phone or addr_doc.phone or "").strip()
	if phone_val:
		result["phone"] = phone_val

	return result


def build_order_payload(so, settings=None, skip_gate: bool = False) -> dict:
	if isinstance(so, str):
		so = frappe.get_doc("Sales Order", so)

	if not settings:
		settings = get_active_settings(throw_if_missing=True)

	if not skip_gate:
		validate_so_fulfillment_gate(so, settings=settings)

	channel = get_so_channel(so)
	channel_cfg = get_channel_config(channel, settings=settings)
	store_id = channel_cfg.get("store_id")
	if not store_id:
		frappe.throw(_("{0} store_id is missing in Shipstation Settings").format(channel))

	so_items = so.get("items") if hasattr(so, "get") else getattr(so, "items", None)
	if not so_items:
		frappe.throw(_("Sales Order {0} has no items").format(so.name))

	shipping_contact = (
		(so.get("custom_shipping_contact") or "").strip()
		if hasattr(so, "custom_shipping_contact")
		else ""
	)
	ship_to_name = (
		shipping_contact
		or (so.get("contact_display") or "").strip()
		or (so.get("customer_name") or "").strip()
	)
	if not ship_to_name:
		frappe.throw(_("Sales Order {0} has no recipient name for shipping").format(so.name))

	bill_to_name = (so.get("customer_name") or "").strip() or (so.get("contact_display") or "").strip()
	if not bill_to_name:
		frappe.throw(_("Sales Order {0} has no customer name for billing").format(so.name))

	shipping_phone = (
		(so.get("custom_shipping_phone") or "").strip()
		or (so.get("contact_phone") or "").strip()
		or (so.get("contact_mobile") or "").strip()
	)
	billing_phone = (so.get("contact_phone") or "").strip() or (so.get("contact_mobile") or "").strip()

	if not so.shipping_address_name:
		frappe.throw(_("Sales Order {0} is missing shipping address").format(so.name))

	if not so.customer_address:
		frappe.throw(_("Sales Order {0} is missing customer billing address").format(so.name))

	ship_to = build_address_dict(
		so.shipping_address_name, ship_to_name, "Shipping Address", phone=shipping_phone
	)
	bill_to = build_address_dict(
		so.customer_address, bill_to_name, "Billing Address", phone=billing_phone
	)

	items_payload = []
	for row in so_items:
		qty = flt(row.qty)
		if qty <= 0:
			frappe.throw(
				_("Item {0} on row {1} has non-positive quantity {2}").format(
					row.item_code or row.name, row.idx, qty
				)
			)

		if qty != cint(qty):
			frappe.throw(
				_("Item {0} on row {1} has fractional quantity {2}. ShipStation requires integer quantities.").format(
					row.item_code or row.name, row.idx, qty
				)
			)

		item_code = (row.item_code or "").strip()
		if not item_code:
			frappe.throw(_("Row {0} is missing item code").format(row.idx))

		custom_sku = frappe.db.get_value("Item", item_code, "custom_sku")
		sku = (custom_sku or item_code).strip()
		if not sku:
			frappe.throw(_("SKU could not be resolved for item {0} on row {1}").format(item_code, row.idx))

		item_name = (row.item_name or item_code).strip()
		if not item_name:
			frappe.throw(_("Item {0} on row {1} is missing item name").format(item_code, row.idx))

		item_dict = {
			"lineItemKey": row.name,
			"sku": sku,
			"name": item_name,
			"quantity": cint(qty),
			"unitPrice": flt(row.rate),
		}
		items_payload.append(item_dict)

	payload = {
		"orderNumber": so.name,
		"orderKey": so.name,
		"orderDate": str(so.transaction_date),
		"orderStatus": "awaiting_shipment",
		"customerName": (so.customer_name or "").strip(),
		"shipTo": ship_to,
		"billTo": bill_to,
		"items": items_payload,
		"advancedOptions": {
			"storeId": str(store_id),
		},
	}

	shipping_rule = (so.get("shipping_rule") or "").strip()
	if shipping_rule:
		payload["requestedShippingService"] = shipping_rule

	customer_notes = (so.get("shipstation_customer_notes") or "").strip()
	if customer_notes:
		payload["customerNotes"] = customer_notes

	internal_notes = (so.get("shipstation_internal_notes") or "").strip()
	if internal_notes:
		payload["internalNotes"] = internal_notes

	return payload


@frappe.whitelist()
def get_shipstation_send_action(sales_order: str) -> str:
	if not sales_order:
		return "none"
	try:
		so = frappe.get_doc("Sales Order", sales_order)
		eligible, _reason, is_update = check_so_fulfillment_gate(so)
		if not eligible:
			return "none"
		return "update" if is_update else "send"
	except Exception:
		return "none"


def _is_timeout_error(exc: Exception) -> bool:
	if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
		return True
	err_str = str(exc).lower()
	return "timeout" in err_str or "timed out" in err_str


def sanitize_error_message(msg: str) -> str:
	if not msg:
		return ""
	cleaned = str(msg)
	cleaned = re.sub(
		r"(?i)(authorization\s*[:=]?\s*)?(basic|bearer)\s+[A-Za-z0-9+/=._-]+",
		"[REDACTED]",
		cleaned,
	)
	cleaned = re.sub(r"(?i)(authorization)\s*[:=]?\s*[^\s,;]+", "[REDACTED]", cleaned)
	cleaned = re.sub(r"(?i)(api[_-]?key|api[_-]?secret)\s*[:=]?\s*[^\s,;]+", "[REDACTED]", cleaned)
	return cleaned[:500]


def _extract_error_status_and_message(exc: Exception) -> tuple[str | int, str]:
	status = "Error"
	message = ""
	if hasattr(exc, "response") and getattr(exc.response, "status_code", None):
		status = exc.response.status_code
		try:
			if hasattr(exc.response, "read"):
				exc.response.read()
			err_json = exc.response.json()
			message = (
				err_json.get("message")
				or err_json.get("ExceptionMessage")
				or getattr(exc.response, "text", str(exc))
			)
		except Exception:
			try:
				message = getattr(exc.response, "text", str(exc))
			except Exception:
				message = str(exc)
	else:
		message = str(exc)
	return status, sanitize_error_message(message)


def _record_failure_comment(so, exc: Exception) -> tuple[str | int, str]:
	status, clean_msg = _extract_error_status_and_message(exc)
	so.add_comment("Comment", text=f"ShipStation send failed: {status} {clean_msg}")
	frappe.db.commit()
	return status, clean_msg


def _post_create_order(client, payload: dict):
	return client.post(endpoint="/orders/createorder", data=json.dumps(payload))


def _find_order_fallback(client, order_number: str) -> dict | None:
	try:
		resp = client.get(endpoint="/orders", payload={"orderNumber": order_number})
		data = resp.json() if callable(getattr(resp, "json", None)) else resp
		orders = data.get("orders", []) if isinstance(data, dict) else []
		for o in orders:
			o_num = o.get("orderNumber") if isinstance(o, dict) else getattr(o, "order_number", None)
			o_key = o.get("orderKey") if isinstance(o, dict) else getattr(o, "order_key", None)
			if str(o_num) == str(order_number) and str(o_key) == str(order_number):
				return o if isinstance(o, dict) else o.__dict__
	except Exception:
		return None
	return None


@frappe.whitelist()
def send_order_to_shipstation(sales_order: str) -> dict:
	if not sales_order:
		frappe.throw(_("Sales Order name is required"))

	so = frappe.get_doc("Sales Order", sales_order)
	settings = get_active_settings(throw_if_missing=True)

	eligible, reason, is_update = check_so_fulfillment_gate(so, settings=settings)
	if not eligible:
		frappe.throw(reason)

	frappe.db.get_value("Sales Order", so.name, "name", for_update=True)

	curr_order_id, curr_order_key = frappe.db.get_value(
		"Sales Order", so.name, ["shipstation_order_id", "shipstation_order_key"]
	)

	if not is_update and curr_order_id:
		frappe.throw(
			_("Sales Order {0} is already linked to ShipStation (Order ID: {1})").format(
				so.name, curr_order_id
			)
		)

	if is_update and (not curr_order_id or not curr_order_key):
		frappe.throw(
			_("Sales Order {0} is missing ShipStation order ID or key for update").format(so.name)
		)

	client = settings.client()
	client.timeout = 30.0
	orig_raise = client.raise_for_status

	def _raise_with_read(response):
		response.read()
		orig_raise(response)

	client.raise_for_status = _raise_with_read

	if is_update:
		try:
			remote_order = client.get_order(curr_order_id)
		except Exception as e:
			status, clean_msg = _record_failure_comment(so, e)
			frappe.throw(_("ShipStation send failed: {0} {1}").format(status, clean_msg))

		remote_status = getattr(remote_order, "order_status", None)
		if not remote_status and isinstance(remote_order, dict):
			remote_status = remote_order.get("orderStatus")
		if remote_status != "awaiting_shipment":
			frappe.throw(
				_("Cannot update order in ShipStation because remote status is '{0}' (expected 'awaiting_shipment')").format(
					remote_status or "unknown"
				)
			)

	payload = build_order_payload(so, settings=settings)

	resp_data = None
	try:
		resp = _post_create_order(client, payload)
		resp_data = resp.json() if callable(getattr(resp, "json", None)) else resp
	except Exception as first_exc:
		if _is_timeout_error(first_exc):
			try:
				resp = _post_create_order(client, payload)
				resp_data = resp.json() if callable(getattr(resp, "json", None)) else resp
			except Exception as second_exc:
				resp_data = _find_order_fallback(client, so.name)
				if not resp_data:
					status, clean_msg = _record_failure_comment(so, second_exc)
					frappe.throw(_("ShipStation send failed: {0} {1}").format(status, clean_msg))
		else:
			status, clean_msg = _record_failure_comment(so, first_exc)
			frappe.throw(_("ShipStation send failed: {0} {1}").format(status, clean_msg))

	returned_order_id = (
		resp_data.get("orderId") if isinstance(resp_data, dict) else getattr(resp_data, "order_id", None)
	)
	returned_order_key = (
		resp_data.get("orderKey") if isinstance(resp_data, dict) else getattr(resp_data, "order_key", None)
	)
	returned_order_status = (
		resp_data.get("orderStatus")
		or (resp_data.get("order_status") if isinstance(resp_data, dict) else None)
		if isinstance(resp_data, dict)
		else getattr(resp_data, "order_status", getattr(resp_data, "orderStatus", None))
	)

	if not returned_order_id:
		frappe.throw(_("ShipStation response missing orderId"))

	if str(returned_order_key) != str(so.name):
		frappe.throw(
			_("ShipStation returned orderKey '{0}' does not match Sales Order name '{1}'").format(
				returned_order_key, so.name
			)
		)

	if str(returned_order_status) != "awaiting_shipment":
		frappe.throw(
			_("ShipStation returned orderStatus '{0}' (expected 'awaiting_shipment')").format(
				returned_order_status
			)
		)

	so.db_set("shipstation_order_id", str(returned_order_id))
	so.db_set("shipstation_order_key", str(returned_order_key))

	channel = get_so_channel(so)
	channel_cfg = get_channel_config(channel, settings=settings)
	store_id = channel_cfg.get("store_id")
	for store in getattr(settings, "shipstation_stores", []):
		if str(store.store_id) == str(store_id):
			so.db_set("shipstation_store_name", store.store_name)
			break

	if is_update:
		comment_text = _("Updated in ShipStation (Order ID: {0}, Key: {1})").format(
			returned_order_id, returned_order_key
		)
	else:
		comment_text = _("Sent to ShipStation (Order ID: {0}, Key: {1})").format(
			returned_order_id, returned_order_key
		)
	so.add_comment("Comment", text=comment_text)

	if not check_store_track_shipments(store_id, settings=settings):
		frappe.msgprint(
			_("Warning: Store {0} is not configured to track shipments in Shipstation Settings.").format(
				store_id
			),
			indicator="orange",
		)

	return {
		"order_id": str(returned_order_id),
		"order_key": str(returned_order_key),
		"is_update": is_update,
	}


def cancel_shipstation_order_job(sales_order_name: str):
	if not sales_order_name:
		return

	so = frappe.get_doc("Sales Order", sales_order_name)
	if not so.shipstation_order_key:
		return

	settings = get_active_settings(throw_if_missing=False)
	if not settings:
		return

	channel = get_so_channel(so)
	channel_cfg = get_channel_config(channel, settings=settings)
	if not channel_cfg.get("send_to_shipstation"):
		return

	if has_active_label(so):
		active_shipment_id = None
		active_tracking = None
		for row in getattr(so, "shipments", []):
			if row.shipment_id and row.shipment_status != "Cancelled":
				active_shipment_id = row.shipment_id
				active_tracking = row.tracking_number
				break
		upsert_exception(
			shipment_id=active_shipment_id,
			order_number=so.name,
			reason="Cancelled After Label",
			matched_sales_orders=so.name,
			source="Outbound",
			order_id=so.shipstation_order_id,
			tracking_number=active_tracking,
		)
		so.add_comment(
			"Comment",
			text=_("ShipStation cancel skipped: label already purchased, exception raised"),
		)
		frappe.db.commit()
		return

	client = settings.client()
	client.timeout = 30.0
	orig_raise = client.raise_for_status

	def _raise_with_read(response):
		response.read()
		orig_raise(response)

	client.raise_for_status = _raise_with_read

	try:
		remote_order = client.get_order(so.shipstation_order_id)
	except Exception as exc:
		status, clean_msg = _extract_error_status_and_message(exc)
		upsert_exception(
			shipment_id=None,
			order_number=so.name,
			reason="Cancel Failed",
			matched_sales_orders=so.name,
			source="Outbound",
			order_id=so.shipstation_order_id,
		)
		so.add_comment("Comment", text=f"ShipStation cancel failed: {status} {clean_msg}")
		frappe.db.commit()
		return

	remote_status = getattr(remote_order, "order_status", None)
	if not remote_status and isinstance(remote_order, dict):
		remote_status = remote_order.get("orderStatus")

	if remote_status == "cancelled":
		so.add_comment(
			"Comment",
			text=_("Cancelled in ShipStation (Order ID: {0}, Key: {1})").format(
				so.shipstation_order_id, so.shipstation_order_key
			),
		)
		frappe.db.commit()
		return

	if remote_status == "shipped":
		upsert_exception(
			shipment_id=getattr(remote_order, "shipment_id", None)
			or (remote_order.get("shipmentId") if isinstance(remote_order, dict) else None),
			order_number=so.name,
			reason="Cancelled After Label",
			matched_sales_orders=so.name,
			source="Outbound",
			order_id=so.shipstation_order_id,
			tracking_number=getattr(remote_order, "tracking_number", None)
			or (remote_order.get("trackingNumber") if isinstance(remote_order, dict) else None),
		)
		so.add_comment(
			"Comment",
			text=_("ShipStation cancel skipped: label already purchased, exception raised"),
		)
		frappe.db.commit()
		return

	if remote_status in ["awaiting_shipment", "awaiting_payment", "on_hold"]:
		cancel_success = False
		try:
			if isinstance(remote_order, dict):
				remote_order["orderStatus"] = "cancelled"
				post_data = json.dumps(remote_order)
			else:
				remote_order.order_status = "cancelled"
				post_data = (
					remote_order.json()
					if hasattr(remote_order, "json") and callable(remote_order.json)
					else json.dumps(remote_order)
				)
			client.post(endpoint="/orders/createorder", data=post_data)
			cancel_success = True
		except Exception:
			try:
				full_payload = build_order_payload(so, settings=settings, skip_gate=True)
				full_payload["orderStatus"] = "cancelled"
				client.post(endpoint="/orders/createorder", data=json.dumps(full_payload))
				cancel_success = True
			except Exception as final_exc:
				status, clean_msg = _extract_error_status_and_message(final_exc)
				upsert_exception(
					shipment_id=None,
					order_number=so.name,
					reason="Cancel Failed",
					matched_sales_orders=so.name,
					source="Outbound",
					order_id=so.shipstation_order_id,
				)
				so.add_comment("Comment", text=f"ShipStation cancel failed: {status} {clean_msg}")
				frappe.db.commit()
				return

		if cancel_success:
			so.add_comment(
				"Comment",
				text=_("Cancelled in ShipStation (Order ID: {0}, Key: {1})").format(
					so.shipstation_order_id, so.shipstation_order_key
				),
			)
			frappe.db.commit()
			return

	upsert_exception(
		shipment_id=None,
		order_number=so.name,
		reason="Cancel Failed",
		matched_sales_orders=so.name,
		source="Outbound",
		order_id=so.shipstation_order_id,
	)
	so.add_comment(
		"Comment",
		text=f"ShipStation cancel failed: Unexpected remote order status '{remote_status}'",
	)
	frappe.db.commit()
