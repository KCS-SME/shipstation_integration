# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import frappe
from frappe import _
from erpnext.stock.doctype.item.item import get_uom_conv_factor
from frappe.utils import flt, get_datetime, getdate
from frappe.utils.safe_exec import is_job_queued
from httpx import HTTPError
from shipstation.models import ShipStationOrder

from shipstation_integration.customer import create_customer
from shipstation_integration.items import create_item

if TYPE_CHECKING:
	from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
	from shipstation.models import ShipStationOrderItem

	from shipstation_integration.shipstation_integration.doctype.shipstation_settings.shipstation_settings import (
		ShipstationSettings,
	)
	from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import (
		ShipstationStore,
	)


def queue_orders():
	if not is_job_queued("shipstation_integration.orders.list_orders", queue="shipstation"):
		frappe.enqueue(
			method="shipstation_integration.orders.list_orders",
			queue="shipstation",
		)


def list_orders(
	settings: "ShipstationSettings" = None,
	last_order_datetime: datetime.datetime = None,
):
	if not settings:
		settings = frappe.get_all("Shipstation Settings", filters={"enabled": True})
	elif not isinstance(settings, list):
		settings = [settings]

	now = datetime.datetime.now(datetime.timezone.utc)
	if last_order_datetime:
		if isinstance(last_order_datetime, str):
			start_dt = get_datetime(last_order_datetime)
		else:
			start_dt = last_order_datetime
		if start_dt.tzinfo is None:
			start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
	else:
		start_dt = now - datetime.timedelta(hours=24)

	start_param = start_dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
	end_param = now.strftime("%Y-%m-%dT%H:%M:%SZ")

	total_fetched = 0
	total_created = 0
	total_updated = 0
	total_skipped = 0
	errors = []

	for sss in settings:
		sss_doc: "ShipstationSettings" = (
			sss if hasattr(sss, "shipstation_stores") else frappe.get_doc("Shipstation Settings", sss.name)
		)
		if not sss_doc.enabled or not sss_doc.enable_legacy_api:
			continue

		client = sss_doc.client()
		client.timeout = 60 * 5

		store: "ShipstationStore"
		for store in sss_doc.shipstation_stores:
			if not store.enable_orders:
				continue

			parameters = {
				"store_id": store.store_id,
				"modify_date_start": start_param,
				"modify_date_end": end_param,
			}

			update_parameter_hook = frappe.get_hooks("update_shipstation_list_order_parameters")
			if update_parameter_hook:
				parameters = frappe.get_attr(update_parameter_hook[0])(parameters)

			try:
				orders = client.list_orders(parameters=parameters)
			except HTTPError as e:
				frappe.log_error(title=f"Error while fetching Shipstation orders for store {store.store_id}", message=frappe.get_traceback())
				errors.append(f"Store {store.store_name or store.store_id} fetch failed: {e}")
				continue
			except Exception as e:
				frappe.log_error(title=f"Unexpected error while fetching Shipstation orders for store {store.store_id}", message=frappe.get_traceback())
				errors.append(f"Store {store.store_name or store.store_id} fetch failed: {e}")
				continue

			try:
				order_iter = iter(orders)
				while True:
					try:
						order = next(order_iter)
					except StopIteration:
						break
					except (HTTPError, Exception) as page_err:
						msg = f"Store {store.store_name or store.store_id} pagination error: {page_err}"
						errors.append(msg)
						frappe.log_error(
							title=f"ShipStation order pagination error for store {store.store_id}",
							message=frappe.get_traceback(),
						)
						break

					total_fetched += 1
					try:
						existing_so = find_existing_sales_order(order)
						if existing_so:
							update_existing_sales_order(existing_so, order, store, sss_doc)
							total_updated += 1
							continue

						is_ebay_store = store.marketplace_name and "ebay" in store.marketplace_name.lower()
						if is_ebay_store:
							total_skipped += 1
							continue

						if validate_order(sss_doc, order, store):
							should_create_order = True

							process_order_hook = frappe.get_hooks("process_shipstation_order")
							if process_order_hook:
								should_create_order = frappe.get_attr(process_order_hook[0])(order, store)

							if should_create_order:
								so_name = create_erpnext_order(order, store, sss_doc)
								if so_name:
									total_created += 1
						else:
							total_skipped += 1
					except Exception as order_err:
						frappe.db.rollback()
						order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
						frappe.log_error(
							title=f"Error syncing ShipStation order {order_ref}",
							message=frappe.get_traceback(),
						)
						errors.append(f"Order {order_ref}: {order_err}")
			except Exception as loop_err:
				errors.append(f"Store {store.store_name or store.store_id}: {loop_err}")

	return {
		"window_start": start_param,
		"window_end": end_param,
		"total_fetched": total_fetched,
		"total_created": total_created,
		"total_updated": total_updated,
		"total_skipped": total_skipped,
		"errors": errors,
	}


def find_existing_sales_order(order: "ShipStationOrder") -> str | None:
	"""
	Locates an existing Sales Order in ERPNext:
	1. Matches eBay Order ID: custom_ebay_order_reference == order_number or order_id
	2. Matches Direct Sales: Sales Order name == order_key or order_number
	3. Matches previously synced: shipstation_order_id == order_id
	4. Matches marketplace_order_id == order_number
	"""
	order_number = getattr(order, "order_number", None)
	order_id = getattr(order, "order_id", None)
	order_key = getattr(order, "order_key", None)

	# 1. Match eBay: custom_ebay_order_reference
	for ref in [order_number, order_id, order_key]:
		if ref:
			ref_str = str(ref).strip()
			so_name = frappe.db.get_value(
				"Sales Order",
				{"custom_ebay_order_reference": ref_str, "docstatus": ["!=", 2]},
				"name",
			)
			if so_name:
				return so_name

	# 2. Match Direct Sales by SO name
	for ref in [order_key, order_number]:
		if ref:
			ref_str = str(ref).strip()
			if frappe.db.exists("Sales Order", ref_str):
				if frappe.db.get_value("Sales Order", ref_str, "docstatus") != 2:
					return ref_str

	# 3. Match by existing shipstation_order_id
	if order_id:
		so_name = frappe.db.get_value(
			"Sales Order",
			{"shipstation_order_id": order_id, "docstatus": ["!=", 2]},
			"name",
		)
		if so_name:
			return so_name

	# 4. Match by marketplace_order_id
	if order_number:
		ref_str = str(order_number).strip()
		so_name = frappe.db.get_value(
			"Sales Order",
			{"marketplace_order_id": ref_str, "docstatus": ["!=", 2]},
			"name",
		)
		if so_name:
			return so_name

	return None


def update_existing_sales_order(
	so_name: str,
	order: "ShipStationOrder",
	store: "ShipstationStore",
	settings: "ShipstationSettings",
):
	"""Syncs ShipStation data into an existing Sales Order without recreating it."""
	updates = {
		"shipstation_order_id": getattr(order, "order_id", None),
		"shipstation_store_name": store.store_name,
		"marketplace": store.marketplace_name,
		"marketplace_order_id": str(getattr(order, "order_number", "")).strip() if getattr(order, "order_number", None) else None,
		"shipstation_customer_notes": getattr(order, "customer_notes", None),
		"shipstation_internal_notes": getattr(order, "internal_notes", None),
		"shipstation_shipment_status": getattr(order, "order_status", None),
		"integration_doctype": "Shipstation Settings",
		"integration_doc": store.parent,
	}

	if getattr(order, "ship_date", None):
		try:
			updates["delivery_date"] = getdate(order.ship_date)
		except Exception:
			pass

	so_meta = frappe.get_meta("Sales Order")
	valid_updates = {k: v for k, v in updates.items() if v is not None and so_meta.has_field(k)}

	for fieldname, val in valid_updates.items():
		frappe.db.set_value("Sales Order", so_name, fieldname, val, update_modified=False)

	frappe.db.commit()
	return so_name


def validate_order(
	settings: "ShipstationSettings",
	order: "ShipStationOrder",
	store: "ShipstationStore",
):
	if not order:
		return False

	# If this store is an eBay store, never create new Sales Orders
	if store.marketplace_name and "ebay" in store.marketplace_name.lower():
		return False

	# If order already exists in ERPNext via eBay reference, do not create new SO
	order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", None)
	if order_ref and frappe.db.exists(
		"Sales Order", {"custom_ebay_order_reference": str(order_ref).strip(), "docstatus": ["!=", 2]}
	):
		return False

	if frappe.db.get_value("Sales Order", {"shipstation_order_id": order.order_id, "docstatus": ["!=", 2]}):
		return False

	if order.order_number and frappe.db.get_value(
		"Sales Order", {"marketplace_order_id": str(order.order_number).strip(), "docstatus": ["!=", 2]}
	):
		return False

	if (
		settings.active_warehouse_ids
		and order.advanced_options.warehouse_id not in settings.active_warehouse_ids
	):
		return False

	if settings.since_date and not getattr(order, "modify_date", None):
		if getdate(order.create_date) < settings.since_date:
			return False

	return True


def create_erpnext_order(
	order: "ShipStationOrder", store: "ShipstationStore", settings: "ShipstationSettings"
) -> str | None:
	if settings.shipstation_user:
		frappe.set_user(settings.shipstation_user)
	customer = (
		frappe.get_cached_doc("Customer", store.customer) if store.customer else create_customer(order)
	)
	if not customer:
		order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
		frappe.throw(_("Customer could not be resolved or created for order {0}.").format(order_ref))

	company = (
		store.company
		or frappe.defaults.get_user_default("Company")
		or frappe.get_cached_value("Global Defaults", None, "default_company")
	)
	cost_center = store.cost_center or (
		frappe.get_cached_value("Company", company, "cost_center") if company else None
	)
	warehouse = store.warehouse or frappe.get_cached_value(
		"Stock Settings", None, "default_warehouse"
	)
	so: "SalesOrder" = frappe.new_doc("Sales Order")
	so.update(
		{
			"shipstation_store_name": store.store_name,
			"shipstation_order_id": order.order_id,
			"shipstation_customer_notes": getattr(order, "customer_notes", None),
			"shipstation_internal_notes": getattr(order, "internal_notes", None),
			"marketplace": store.marketplace_name,
			"marketplace_order_id": order.order_number,
			"customer": customer.name,
			"customer_name": order.customer_email,
			"company": company,
			"transaction_date": getdate(order.order_date),
			"delivery_date": getdate(order.ship_date),
			"shipping_address_name": getattr(customer, "customer_primary_address", None),
			"integration_doctype": "Shipstation Settings",
			"integration_doc": store.parent,
			"has_pii": True,
			"currency": store.currency,
		}
	)
	if store.sales_partner:
		so.sales_partner = store.sales_partner

	if store.get("is_amazon_store"):
		update_hook = frappe.get_hooks("update_shipstation_amazon_order")
		if update_hook:
			so = frappe.get_attr(update_hook[0])(store, order, so)
	elif store.get("is_shopify_store"):
		update_hook = frappe.get_hooks("update_shipstation_shopify_order")
		if update_hook:
			so = frappe.get_attr(update_hook[0])(store, order, so)

	# using `hasattr` over `getattr` to use type annotations
	order_items = order.items if hasattr(order, "items") else []
	if not order_items:
		return

	process_order_items_hook = frappe.get_hooks("process_shipstation_order_items")
	if process_order_items_hook:
		order_items = frappe.get_attr(process_order_items_hook[0])(order_items)

	discount_amount = 0.0
	for item in order_items:
		if item.quantity < 1:
			continue

		rate = flt(item.unit_price) if hasattr(item, "unit_price") else 0.0

		# the only way to identify marketplace discounts via the Shipstation API is
		# to find it using the `line_item_key` string
		if item.line_item_key == "discount":
			discount_amount += abs(rate * item.quantity)
			continue

		settings = frappe.get_doc("Shipstation Settings", store.parent)
		stock_item = create_item(item, settings=settings, store=store)
		uom = stock_item.sales_uom or stock_item.stock_uom
		conversion_factor = (
			1 if uom == stock_item.stock_uom else get_uom_conv_factor(uom, stock_item.stock_uom)
		)
		item_notes = get_item_notes(item)
		so.append(
			"items",
			{
				"item_code": stock_item.item_code,
				"qty": item.quantity,
				"uom": uom,
				"conversion_factor": conversion_factor,
				"rate": rate,
				"warehouse": warehouse,
				"shipstation_order_item_id": item.order_item_id,
				"shipstation_item_notes": item_notes,
			},
		)

	if not so.get("items"):
		return

	so.dont_update_if_missing = ["customer_name", "base_total_in_words"]

	tax_account = store.tax_account or frappe.db.get_value(
		"Account", {"company": company, "is_group": 0, "disabled": 0, "account_type": "Tax"}, "name"
	)
	shipping_account = store.shipping_income_account or frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "disabled": 0, "account_type": "Income Account"},
		"name",
	) or frappe.get_cached_value("Company", company, "default_income_account")
	difference_account = store.difference_account or frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 0, "disabled": 0, "account_type": "Discount"},
		"name",
	) or shipping_account

	if order.tax_amount:
		if not tax_account:
			order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
			frappe.throw(
				_("Tax account not configured for company {0}. Cannot add tax of {1} for order {2}.").format(
					company, order.tax_amount, order_ref
				)
			)
		so.sales_tax_total = flt(order.tax_amount)
		so.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": tax_account,
				"description": "Shipstation Tax Amount",
				"tax_amount": order.tax_amount,
				"cost_center": cost_center,
			},
		)

	if order.shipping_amount:
		if not shipping_account:
			order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
			frappe.throw(
				_("Shipping income account not configured for company {0}. Cannot add shipping charge of {1} for order {2}.").format(
					company, order.shipping_amount, order_ref
				)
			)
		so.shipping_revenue = flt(order.shipping_amount)
		so.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": shipping_account,
				"description": "Shipstation Shipping Amount",
				"tax_amount": order.shipping_amount,
				"cost_center": cost_center,
			},
		)

	so.save()
	if store.customer:
		so.customer_name = order.customer_email
	# coupons
	if order.amount_paid and Decimal(so.grand_total).quantize(Decimal(".01")) != order.amount_paid:
		difference_amount = Decimal(Decimal(so.grand_total).quantize(Decimal(".01")) - order.amount_paid)
		so.shipstation_discount = difference_amount
		account = difference_account
		# if the shipping amount is noted but not charged (FBA orders), this correctly offsets it
		if difference_amount == order.shipping_amount:
			account = shipping_account
		if not account:
			order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
			frappe.throw(
				_("Discount/Difference account not configured for company {0}. Cannot apply difference of {1} for order {2}.").format(
					company, difference_amount, order_ref
				)
			)
		so.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": "Shipstation Difference Amount",
				"tax_amount": -1 * difference_amount,
				"cost_center": cost_center,
			},
		)

	if order.tax_amount and store.withholding:
		if not tax_account:
			order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
			frappe.throw(
				_("Tax account not configured for company {0}. Cannot apply tax withholding for order {1}.").format(
					company, order_ref
				)
			)
		# reverse withholding
		so.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": tax_account,
				"description": "Shipstation Tax Amount",
				"tax_amount": order.tax_amount * -1,
				"cost_center": cost_center,
			},
		)

	if discount_amount > 0:
		so.apply_discount_on = "Grand Total"
		so.discount_amount = discount_amount

	if so.sales_partner and store.apply_commission:
		so.calculate_commission()
		if so.total_commission:
			if not store.commission_account:
				order_ref = getattr(order, "order_number", None) or getattr(order, "order_id", "unknown")
				frappe.throw(
					_("Commission account not configured on store {0}. Cannot apply commission for order {1}.").format(
						store.store_name or store.store_id, order_ref
					)
				)
			so.append(
				"taxes",
				{
					"charge_type": "Actual",
					"account_head": store.commission_account,
					"cost_center": cost_center,
					"description": f"Commission of {so.get_formatted('total_commission')}",
					"tax_amount": -(so.total_commission),
					"included_in_paid_amount": 1,
				},
			)

	so.save()

	before_submit_hook = frappe.get_hooks("update_shipstation_order_before_submit")
	if before_submit_hook:
		so = frappe.get_attr(before_submit_hook[0])(store, so, order)
		if so:
			so.save()
	if so:
		so.submit()
		frappe.db.commit()

	after_submit_hook = frappe.get_hooks("update_shipstation_order_after_submit")
	if after_submit_hook:
		frappe.get_attr(after_submit_hook[0])(store, so, order)
		frappe.db.commit()

	if order.tag_ids:
		for tag_id in order.tag_ids:
			tag_name = frappe.db.exists("Tag", {"tag_id": tag_id})
			if tag_name:
				so.add_tag(tag_name)
				# frappe.db.commit()

	return so.name if so else None


def get_item_notes(item: "ShipStationOrderItem"):
	notes = None
	item_options = item.options if hasattr(item, "options") else None
	if item_options:
		for option in item_options:
			if option.name == "Description":
				notes = option.value
				break
	return notes


def create_order_from_webhook(order: dict, store: str, settings: str):
	store = frappe.get_doc("Shipstation Store", store)
	settings = frappe.get_doc("Shipstation Settings", settings)
	order = ShipStationOrder().json(order)

	if not store.enable_orders:
		return

	if not validate_order(settings, order, store):
		return

	should_create_order = True

	process_order_hook = frappe.get_hooks("process_shipstation_order")
	if process_order_hook:
		should_create_order = frappe.get_attr(process_order_hook[0])(order, store)

	if not should_create_order:
		return

	create_erpnext_order(order, store, settings)
