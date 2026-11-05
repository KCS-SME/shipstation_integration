# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

import frappe

from shipstation_integration.shipstation import v1_api

V1_SHIPMENTS_ENDPOINT = "/shipments"

V1_CALLBACK_PARAM_MAP = {
	"storeID": "storeId",
	"storeId": "storeId",
	"orderNumber": "orderNumber",
	"orderId": "orderId",
	"trackingNumber": "trackingNumber",
	"batchId": "batchId",
	"importBatch": "importBatch",
}


def build_shipments_query(store_id: str, callback_params: dict | None = None) -> dict:
	query = {"storeId": str(store_id), "includeShipmentItems": "true"}
	for callback_param, value in (callback_params or {}).items():
		query_param = V1_CALLBACK_PARAM_MAP.get(callback_param)
		if not query_param or query_param == "storeId" or value in (None, ""):
			continue
		query[query_param] = str(value)
	return query


def fetch_shipments(client, query: dict) -> list[dict]:
	try:
		return v1_api.paginate(client, V1_SHIPMENTS_ENDPOINT, query, "shipments", "shipment")
	except v1_api.ResultLimitExceeded as exc:
		frappe.log_error(title="ShipStation V1 shipment lookup failed", message=str(exc))
		frappe.throw(str(exc))
