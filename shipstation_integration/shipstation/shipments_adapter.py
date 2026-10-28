from shipstation_integration.items import get_item_code_from_sku

LABEL_SHIPMENT_SOURCE = "ShipStation Label"
EXTERNAL_SHIPMENT_SOURCE = "External"


def trim_date(value):
	if not value:
		return value
	return str(value)[:19].replace("T", " ")


def map_items(items):
	res = []
	for it in items or []:
		if isinstance(it, dict):
			sku = it.get("sku")
			qty = it.get("quantity")
			name = it.get("name")
		else:
			sku = getattr(it, "sku", None)
			qty = getattr(it, "quantity", None)
			name = getattr(it, "name", None)
		res.append(
			{
				"sku": sku,
				"item_code": get_item_code_from_sku(sku),
				"name": name,
				"quantity": float(qty) if qty is not None else None,
			}
		)
	return res


def from_api_dict(d):
	if isinstance(d, dict):
		shipment_id = d.get("shipmentId") or d.get("shipment_id")
		order_id = d.get("orderId") or d.get("order_id")
		order_number = d.get("orderNumber") or d.get("order_number")
		voided = d.get("voided")
		tracking_number = d.get("trackingNumber") or d.get("tracking_number")
		carrier_code = d.get("carrierCode") or d.get("carrier_code")
		service_code = d.get("serviceCode") or d.get("service_code")
		ship_date = d.get("shipDate") or d.get("ship_date")
		shipment_cost = d.get("shipmentCost") or d.get("shipment_cost")
		label_id = d.get("labelId") or d.get("label_id")
		items = d.get("shipmentItems") or d.get("shipment_items")
	else:
		shipment_id = getattr(d, "shipment_id", None) or getattr(d, "shipmentId", None)
		order_id = getattr(d, "order_id", None) or getattr(d, "orderId", None)
		order_number = getattr(d, "order_number", None) or getattr(d, "orderNumber", None)
		voided = getattr(d, "voided", False)
		tracking_number = getattr(d, "tracking_number", None) or getattr(d, "trackingNumber", None)
		carrier_code = getattr(d, "carrier_code", None) or getattr(d, "carrierCode", None)
		service_code = getattr(d, "service_code", None) or getattr(d, "serviceCode", None)
		ship_date = getattr(d, "ship_date", None) or getattr(d, "shipDate", None)
		shipment_cost = getattr(d, "shipment_cost", None) or getattr(d, "shipmentCost", None)
		label_id = getattr(d, "label_id", None) or getattr(d, "labelId", None)
		items = getattr(d, "shipment_items", None) or getattr(d, "shipmentItems", None)

	return {
		"shipment_id": str(shipment_id) if shipment_id is not None else None,
		"order_id": str(order_id) if order_id is not None else None,
		"order_number": order_number,
		"shipment_source": LABEL_SHIPMENT_SOURCE,
		"voided": bool(voided),
		"tracking_number": tracking_number,
		"carrier_code": carrier_code,
		"service_code": service_code,
		"ship_date": trim_date(ship_date),
		"shipment_cost": shipment_cost,
		"label_id": label_id,
		"items": map_items(items),
	}
