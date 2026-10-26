# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

from typing import TYPE_CHECKING, Optional

import frappe
from frappe.utils import flt
from shipstation.models import ShipStationItem, ShipStationOrderItem

if TYPE_CHECKING:
	from erpnext.stock.doctype.item.item import Item

	from shipstation_integration.shipstation_integration.doctype.shipstation_settings.shipstation_settings import (
		ShipstationSettings,
	)
	from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import (
		ShipstationStore,
	)


def get_item_code_from_sku(sku):
	if not sku:
		return None
	sku_clean = sku.strip()
	item_code = frappe.db.get_value("Item", {"item_code": sku_clean})
	if item_code:
		return item_code
	return frappe.db.get_value("Item", {"custom_sku": sku_clean})


def create_item(
	product: ShipStationItem | ShipStationOrderItem,
	settings: "ShipstationSettings",
	store: Optional["ShipstationStore"] = None,
) -> "Item":

	if settings.shipstation_user:
		frappe.set_user(settings.shipstation_user)

	sku = (getattr(product, "sku", None) or "").strip()
	raw_name = getattr(product, "name", None) or ""
	item_name = raw_name[:140] if raw_name else sku

	item_code = None
	if sku:
		item_code = frappe.db.get_value("Item", {"item_code": sku})
		if not item_code:
			item_code = frappe.db.get_value("Item", {"custom_sku": sku})
	elif item_name:
		item_code = frappe.db.get_value("Item", {"item_name": item_name.strip()})

	if item_code:
		item: "Item" = frappe.get_doc("Item", item_code)
		if item.disabled:
			item.disabled = False
			item.save()
		return item

	weight_per_unit, weight_uom = 1.0, "Ounce"
	if isinstance(product, ShipStationItem):
		weight_per_unit = flt(getattr(product, "weight_oz", 1))
	elif isinstance(product, ShipStationOrderItem):
		weight = product.weight if hasattr(product, "weight") else frappe._dict()
		if weight:
			weight_per_unit = flt(weight.value) if weight else 1
			weight_uom = weight.units.title() if weight and weight.units else "Ounce"

			if weight_uom.lower() == "ounces":
				weight_uom = "Ounce"
			elif weight_uom.lower() == "grams":
				weight_uom = "Gram"
			elif weight_uom.lower() in ("pound", "pounds", "lb", "lbs"):
				weight_uom = "Pound"

			if settings.weight_conversion == "Convert to Gram" and weight_uom == "Ounce":
				weight_per_unit = flt(weight_per_unit * 28.3495, 2)
				weight_uom = "Gram"
			elif settings.weight_conversion == "Convert to Gram" and weight_uom == "Pound":
				weight_per_unit = flt(weight_per_unit * 453.592, 2)
				weight_uom = "Gram"
			elif settings.weight_conversion == "Convert to Ounce" and weight_uom == "Gram":
				weight_per_unit = flt(weight_per_unit * 0.035274, 2)
				weight_uom = "Ounce"

	item: "Item" = frappe.new_doc("Item")
	item_data = {
		"item_code": sku or item_name,
		"custom_sku": sku or None,
		"custom_repairability_status": "Repairable",
		"item_name": item_name,
		"item_group": settings.default_item_group,
		"is_stock_item": True,
		"include_item_in_manufacturing": False,
		"description": getattr(product, "internal_notes", raw_name or sku),
		"weight_per_unit": weight_per_unit,
		"weight_uom": weight_uom,
		"end_of_life": "",
	}
	if item.meta.has_field("custom_testing_status"):
		item_data["custom_testing_status"] = "Ready for Sale"
	item.update(item_data)

	company = (
		(store.company if store and store.company else None)
		or frappe.defaults.get_user_default("Company")
		or frappe.get_cached_value("Global Defaults", None, "default_company")
	)

	if store:
		item.update(
			{
				"integration_doctype": "Shipstation Settings",
				"integration_doc": store.parent,
				"store": store.name,
			}
		)

	if company and not item.get("item_defaults"):
		cost_center = store.cost_center if store else None
		expense_account = store.expense_account if store else None
		sales_account = store.sales_account if store else None
		default_price_list = "ShipStation" if frappe.db.exists("Price List", "ShipStation") else None
		item.set(
			"item_defaults",
			[
				{
					"company": company,
					"default_price_list": default_price_list,
					"default_warehouse": "",
					"buying_cost_center": cost_center,
					"selling_cost_center": cost_center,
					"expense_account": expense_account,
					"income_account": sales_account,
				}
			],
		)

	before_save_hook = frappe.get_hooks("update_shipstation_item_before_save")
	if before_save_hook:
		item = frappe.get_attr(before_save_hook[0])(store, item)

	try:
		item.save()
		frappe.db.commit()
	except frappe.TimestampMismatchError:
		frappe.log_error(
			title=f"Timestamp Mismatch Error for Item {item_name}", message=frappe.get_traceback()
		)
		item.reload()
		item.save()
		frappe.db.commit()
	except Exception:
		frappe.log_error(title=f"Error saving Item {item_name}", message=frappe.get_traceback())
		raise

	return item
