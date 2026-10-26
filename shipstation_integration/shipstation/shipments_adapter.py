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
		sku = it.get("sku")
		qty = it.get("quantity")
		res.append(
			{
				"sku": sku,
				"item_code": get_item_code_from_sku(sku),
				"name": it.get("name"),
				"quantity": float(qty) if qty is not None else None,
			}
		)
	return res


def from_api_dict(d):
	shipment_id = d.get("shipmentId")
	order_id = d.get("orderId")
	return {
		"shipment_id": str(shipment_id) if shipment_id is not None else None,
		"order_id": str(order_id) if order_id is not None else None,
		"order_number": d.get("orderNumber"),
		"shipment_source": LABEL_SHIPMENT_SOURCE,
		"voided": bool(d.get("voided")),
		"tracking_number": d.get("trackingNumber"),
		"carrier_code": d.get("carrierCode"),
		"service_code": d.get("serviceCode"),
		"ship_date": trim_date(d.get("shipDate")),
		"shipment_cost": d.get("shipmentCost"),
		"label_id": d.get("labelId"),
		"items": map_items(d.get("shipmentItems")),
	}
