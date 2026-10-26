# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

from typing import TYPE_CHECKING

import frappe
from frappe.exceptions import DuplicateEntryError
from frappe.utils import getdate, parse_addr

if TYPE_CHECKING:
	from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
	from frappe.contacts.doctype.address.address import Address
	from frappe.contacts.doctype.contact.contact import Contact
	from shipstation.models import ShipStationAddress, ShipStationOrder

	from shipstation_integration.shipstation_integration.doctype.shipstation_store.shipstation_store import (
		ShipstationStore,
	)


def update_customer_details(
	existing_so: str, order: "ShipStationOrder", store: "ShipstationStore"
):
	existing_so_doc: "SalesOrder" = frappe.get_doc("Sales Order", existing_so)

	email_id, _ = parse_addr(existing_so_doc.amazon_customer)
	if email_id:
		contact = create_contact(order, email_id)
		existing_so_doc.contact_person = contact.name

	existing_so_doc.update(
		{
			"shipstation_order_id": order.order_id,
			"shipstation_store_name": store.store_name,
			"shipstation_customer_notes": getattr(order, "customer_notes", None),
			"shipstation_internal_notes": getattr(order, "internal_notes", None),
			"marketplace_order_id": order.order_number,
			"delivery_date": getdate(order.ship_date),
			"has_pii": True,
			"integration_doctype": "Shipstation Settings",
			"integration_doc": store.parent,
		}
	)

	if order.bill_to and order.bill_to.street1:
		if existing_so_doc.customer_address:
			bill_address = update_address(
				order.bill_to,
				existing_so_doc.customer_address,
				order.customer_email,
				"Billing",
			)
		else:
			bill_address = create_address(
				order.bill_to,
				existing_so_doc.amazon_customer,
				order.customer_email,
				"Billing",
			)
			existing_so_doc.customer_address = bill_address.name
	if order.ship_to and order.ship_to.street1:
		if existing_so_doc.shipping_address_name:
			ship_address = update_address(
				order.ship_to,
				existing_so_doc.shipping_address_name,
				order.customer_email,
				"Shipping",
			)
		else:
			ship_address = create_address(
				order.ship_to,
				existing_so_doc.amazon_customer,
				order.customer_email,
				"Shipping",
			)
			existing_so_doc.shipping_address_name = ship_address.name

	existing_so_doc.flags.ignore_validate_update_after_submit = True
	existing_so_doc.run_method("set_customer_address")
	existing_so_doc.save()
	return existing_so_doc


def create_address(address: "ShipStationAddress", customer: str, email: str, address_type: str):
	addr: "Address" = frappe.new_doc("Address")
	addr.append("links", {"link_doctype": "Customer", "link_name": customer})
	apply_address_fields(address, addr, email, address_type)
	return addr


def update_address(
	address: "ShipStationAddress", address_name: str, email: str, address_type: str
):
	addr: "Address" = frappe.get_doc("Address", address_name)
	apply_address_fields(address, addr, email, address_type)
	return addr


def apply_address_fields(
	address: "ShipStationAddress", addr: "Address", email: str, address_type: str
):
	addr.address_type = address_type
	addr.address_line1 = address.street1
	addr.address_line2 = address.street2
	addr.address_line3 = address.street3
	addr.city = address.city
	addr.state = address.state
	addr.pincode = address.postal_code
	addr.country = frappe.get_cached_value("Country", {"code": address.country}, "name")
	addr.phone = address.phone
	addr.email = email
	try:
		addr.save()
		return addr
	except Exception as e:
		frappe.log_error(title="Error saving Shipstation Address", message=e)


def create_customer(order: "ShipStationOrder"):
	ship_to = getattr(order, "ship_to", None)
	ship_to_name = getattr(ship_to, "name", None) if ship_to else None
	customer_name = (
		ship_to_name or order.customer_email or order.customer_id or frappe.generate_hash("", 10)
	)

	if (
		frappe.get_cached_value("Selling Settings", "Selling Settings", "cust_master_name")
		!= "Customer Name"
	):
		customer = frappe.db.get_value("Customer", {"customer_name": customer_name})
		return frappe.get_doc("Customer", customer)
	else:
		if frappe.db.exists("Customer", customer_name):
			return frappe.get_doc("Customer", customer_name)

	cust = frappe.new_doc("Customer")
	cust.shipstation_customer_id = order.customer_id
	cust.customer_name = customer_name
	cust.customer_type = "Individual"
	cust.customer_group = "ShipStation" if frappe.db.exists("Customer Group", "ShipStation") else (
		frappe.db.get_single_value("Selling Settings", "customer_group") or "All Customer Groups"
	)
	cust.territory = "United States" if frappe.db.exists("Territory", "United States") else (
		frappe.db.get_single_value("Selling Settings", "territory") or "All Territories"
	)
	try:
		cust.save()
		frappe.db.commit()
	# this is a bad way to do this but its unclear why its happening
	except DuplicateEntryError as e:
		frappe.log_error(title="DuplicateEntryError on Customer", message=e)
		customer = frappe.db.get_value("Customer", {"customer_name": customer_name})
		return frappe.get_doc("Customer", customer_name)
	except Exception as e:
		frappe.log_error(title="Error creating Shipstation Customer", message=e)
		raise e

	email_id, _ = parse_addr(customer_name)
	if email_id:
		customer_primary_contact = create_contact(order, email_id)
		if customer_primary_contact:
			cust.customer_primary_contact = customer_primary_contact.name

	if getattr(order, "ship_to", None) and getattr(order.ship_to, "street1", None):
		create_address(order.ship_to, customer_name, order.customer_email, "Shipping")
	if getattr(order, "bill_to", None) and getattr(order.bill_to, "street1", None):
		create_address(order.bill_to, order.customer_username, order.customer_email, "Billing")

	try:
		cust.save()
		frappe.db.commit()
		return cust
	except Exception as e:
		frappe.log_error(title="Error saving Shipstation Customer", message=frappe.get_traceback())
		raise


def create_contact(order: "ShipStationOrder", customer_name: str):
	contact = frappe.get_value("Contact Email", {"email_id": customer_name}, "parent")
	if contact:
		return frappe._dict({"name": contact})
	cont: "Contact" = frappe.new_doc("Contact")
	bill_to = getattr(order, "bill_to", None)
	bill_name = getattr(bill_to, "name", None) if bill_to else None
	cont.first_name = bill_name or "Not Provided"
	for char in "<>":
		cont.first_name = cont.first_name.replace(char, "")
	if customer_name:
		cont.append("email_ids", {"email_id": customer_name})
		cont.append("links", {"link_doctype": "Customer", "link_name": customer_name})
	try:
		cont.save()
		frappe.db.commit()
		return cont
	except Exception as e:
		frappe.log_error(title="Error saving Shipstation Contact", message=e)


def overwrite_validate_phone_number(data, throw=False):
	return True
