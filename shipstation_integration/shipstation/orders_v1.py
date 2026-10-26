# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

from urllib.parse import urlparse

import frappe

from shipstation_integration.shipstation import shipments_adapter, v1_api

V1_ORDERS_ENDPOINT = "/orders"
V1_ORDER_STATUS_SHIPPED = "shipped"

V1_ORDERS_CALLBACK_PARAM_MAP = {
	"storeID": "storeId",
	"storeId": "storeId",
	"orderNumber": "orderNumber",
	"orderStatus": "orderStatus",
	"customerName": "customerName",
	"itemKeyword": "itemKeyword",
	"createDateStart": "createDateStart",
	"createDateEnd": "createDateEnd",
	"modifyDateStart": "modifyDateStart",
	"modifyDateEnd": "modifyDateEnd",
	"orderDateStart": "orderDateStart",
	"orderDateEnd": "orderDateEnd",
	"paymentDateStart": "paymentDateStart",
	"paymentDateEnd": "paymentDateEnd",
	"page": "page",
	"pageSize": "pageSize",
	"batchId": "batchId",
	"importBatch": "importBatch",
}

EXTERNAL_SHIPMENT_SOURCE = shipments_adapter.EXTERNAL_SHIPMENT_SOURCE


def build_order_notify_query(store_id: str, callback_params: dict | None = None) -> dict:
	query = {"storeId": str(store_id)}
	for callback_param, value in (callback_params or {}).items():
		query_param = V1_ORDERS_CALLBACK_PARAM_MAP.get(callback_param)
		if not query_param or query_param == "storeId" or value in (None, ""):
			continue
		query[query_param] = str(value)
	return query


def fetch_order_notify_orders(
	client, store_id: str, resource_url: str | None = None, callback_params: dict | None = None
) -> list[dict]:
	parsed_url = urlparse(resource_url or "")
	path_parts = [p for p in parsed_url.path.strip("/").split("/") if p]

	if path_parts and path_parts[0] != "orders":
		frappe.log_error(
			title="ShipStation V1 order lookup failed",
			message=f"Unsupported resource URL path for ORDER_NOTIFY: {parsed_url.path!r}.",
		)
		return []

	if len(path_parts) > 2 or (len(path_parts) == 2 and not path_parts[1].isdigit()):
		frappe.log_error(
			title="ShipStation V1 order lookup failed",
			message=f"Malformed order resource URL path: {parsed_url.path!r}.",
		)
		return []

	order_id = None
	if len(path_parts) == 2 and path_parts[0] == "orders" and path_parts[1].isdigit():
		order_id = path_parts[1]
	elif (callback_params or {}).get("orderId") and str(callback_params.get("orderId")).isdigit():
		order_id = str(callback_params.get("orderId"))

	if order_id:
		endpoint = f"{V1_ORDERS_ENDPOINT}/{order_id}"
		response = client.get(endpoint=endpoint)
		response.raise_for_status()
		data = response.json()
		if not isinstance(data, dict):
			frappe.log_error(
				title="ShipStation V1 order lookup failed",
				message=f"Expected dictionary response for order {order_id}, got {type(data).__name__}.",
			)
			return []

		order_store_id = str(data.get("advancedOptions", {}).get("storeId") or data.get("storeId") or "")
		if order_store_id and order_store_id != str(store_id):
			frappe.log_error(
				title="ShipStation ORDER_NOTIFY order store mismatch",
				message=(
					f"Order {data.get('orderId')} belongs to store {order_store_id}, "
					f"not validated store {store_id}."
				),
			)
			return []
		return [data]

	query = build_order_notify_query(store_id, callback_params)
	response = client.get(endpoint=V1_ORDERS_ENDPOINT, payload=query)
	response.raise_for_status()
	data = response.json()
	if not isinstance(data, dict):
		frappe.log_error(
			title="ShipStation V1 order lookup failed",
			message=f"Expected dictionary response for orders query, got {type(data).__name__}.",
		)
		return []

	orders = data.get("orders") or []
	if not isinstance(orders, list):
		frappe.log_error(
			title="ShipStation V1 order lookup failed",
			message=f"Expected list for 'orders' key, got {type(orders).__name__}.",
		)
		return []

	matching_orders = []
	for order in orders:
		if not isinstance(order, dict):
			continue
		order_store_id = str(order.get("advancedOptions", {}).get("storeId") or order.get("storeId") or "")
		if order_store_id and order_store_id != str(store_id):
			continue
		matching_orders.append(order)

	return matching_orders


def build_orders_query(store_id: str, start: str, end: str) -> dict:
	return {
		"storeId": str(store_id),
		"orderStatus": V1_ORDER_STATUS_SHIPPED,
		"modifyDateStart": start,
		"modifyDateEnd": end,
	}


def fetch_orders(client, query: dict) -> list[dict]:
	return v1_api.paginate(client, V1_ORDERS_ENDPOINT, query, "orders", "order")


def from_api_dict(order: dict) -> dict:
	order_id = order.get("orderId")
	return {
		"shipment_id": None,
		"order_id": str(order_id) if order_id is not None else None,
		"order_number": order.get("orderNumber"),
		"order_status": order.get("orderStatus"),
		"shipment_source": EXTERNAL_SHIPMENT_SOURCE,
		"voided": False,
		"tracking_number": None,
		"carrier_code": order.get("carrierCode"),
		"service_code": order.get("serviceCode"),
		"ship_date": shipments_adapter.trim_date(order.get("shipDate")),
		"modify_date": shipments_adapter.trim_date(order.get("modifyDate")),
		"shipment_cost": None,
		"label_id": None,
		"items": shipments_adapter.map_items(order.get("items")),
	}
