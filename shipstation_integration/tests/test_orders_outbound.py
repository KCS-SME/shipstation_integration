# Copyright (c) 2026, AgriTheory and Contributors
# See license.txt

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.desk.form.meta import get_code_files_via_hooks
from frappe.tests.utils import FrappeTestCase

from shipstation_integration.shipstation.orders_outbound import (
	build_order_payload,
	check_so_fulfillment_gate,
	get_channel_config,
	get_shipstation_send_action,
	get_so_channel,
	send_order_to_shipstation,
	validate_so_fulfillment_gate,
)


class TestOrdersOutbound(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._created_docs = []

	def tearDown(self):
		for dt, dn in reversed(self._created_docs):
			if frappe.db.exists(dt, dn):
				try:
					frappe.delete_doc(dt, dn, force=True)
				except Exception:
					pass
		super().tearDown()

	# -------------------------------------------------------------
	# 1. Frontend and Registration Tests
	# -------------------------------------------------------------

	def test_sales_order_js_hook_registration(self):
		"""Verify that sales_order.js is registered via doctype_js for Sales Order."""
		hook_files = get_code_files_via_hooks("doctype_js", "Sales Order")
		expected_suffix = "shipstation_integration/public/js/sales_order.js"
		self.assertTrue(
			any(f.endswith(expected_suffix) for f in hook_files),
			f"Expected {expected_suffix} in registered doctype_js hooks: {hook_files}",
		)

	def test_sales_order_js_contents(self):
		"""Verify that sales_order.js contains the exact button label and action methods."""
		hook_files = [
			f for f in get_code_files_via_hooks("doctype_js", "Sales Order")
			if f.endswith("shipstation_integration/public/js/sales_order.js")
		]
		self.assertTrue(len(hook_files) > 0)
		content = frappe.read_file(hook_files[0])
		self.assertIn("Send Order to ShipStation", content)
		self.assertIn("Update in ShipStation", content)
		self.assertIn("get_shipstation_send_action", content)
		self.assertIn("send_order_to_shipstation", content)
		self.assertIn("frm.page.remove_inner_button", content)

	# -------------------------------------------------------------
	# 2. Channel Classification Tests
	# -------------------------------------------------------------

	def test_channel_classification_precedence(self):
		"""Verify get_so_channel prioritizes eBay, then Shopify, else Direct Sales."""
		mock_so = frappe._dict({
			"name": "TEST-SO-CH",
			"custom_ebay_order_reference": None,
			"custom_shopify_order_reference": None,
		})
		self.assertEqual(get_so_channel(mock_so), "Direct Sales")

		mock_so.custom_ebay_order_reference = "EBAY-123"
		self.assertEqual(get_so_channel(mock_so), "eBay")

		mock_so.custom_ebay_order_reference = None
		mock_so.custom_shopify_order_reference = "SHOP-456"
		self.assertEqual(get_so_channel(mock_so), "Shopify")

		mock_so.custom_ebay_order_reference = "EBAY-123"
		mock_so.custom_shopify_order_reference = "SHOP-456"
		with self.assertRaises(frappe.ValidationError):
			get_so_channel(mock_so)

	# -------------------------------------------------------------
	# 3. Fulfillment Gate and Button Visibility Conditions
	# -------------------------------------------------------------

	def _build_mock_settings(self, channels=None, enabled=1):
		settings = frappe._dict({
			"name": "Mock ShipStation Settings",
			"enabled": enabled,
			"channels": channels or [],
			"shipstation_stores": [
				frappe._dict({"store_id": "4988058", "store_name": "Manual Orders", "track_shipments": 1}),
				frappe._dict({"store_id": "4988125", "store_name": "Api Shipments", "track_shipments": 1}),
			],
		})
		return settings

	def test_gate_hides_button_on_draft_and_cancelled_orders(self):
		"""Button must not appear on draft or cancelled orders."""
		settings = self._build_mock_settings(channels=[
			frappe._dict({"channel": "Direct Sales", "send_to_shipstation": 1, "store_id": "4988058"})
		])
		so = frappe._dict({
			"name": "SO-DRAFT",
			"docstatus": 0,
			"status": "Draft",
			"per_delivered": 0,
			"shipstation_order_id": None,
			"shipstation_order_key": None,
		})
		ok, reason, is_up = check_so_fulfillment_gate(so, settings=settings)
		self.assertFalse(ok)
		self.assertIn("not submitted", reason)

		so.docstatus = 2
		so.status = "Cancelled"
		ok, reason, is_up = check_so_fulfillment_gate(so, settings=settings)
		self.assertFalse(ok)

	def test_gate_hides_button_on_closed_completed_fully_delivered(self):
		"""Button must not appear when closed, completed, or 100% delivered."""
		settings = self._build_mock_settings(channels=[
			frappe._dict({"channel": "Direct Sales", "send_to_shipstation": 1, "store_id": "4988058"})
		])
		so = frappe._dict({
			"name": "SO-SUBMITTED",
			"docstatus": 1,
			"status": "Completed",
			"per_delivered": 0,
			"shipstation_order_id": None,
			"shipstation_order_key": None,
		})
		ok, reason, _ = check_so_fulfillment_gate(so, settings=settings)
		self.assertFalse(ok)

		so.status = "To Deliver and Bill"
		so.per_delivered = 100.0
		ok, reason, _ = check_so_fulfillment_gate(so, settings=settings)
		self.assertFalse(ok)
		self.assertIn("already fully delivered", reason)

	def test_gate_respects_channel_enabled_disabled_settings(self):
		"""Button visibility strictly respects channel configuration in ShipStation Settings."""
		# Scenario A: Direct Sales enabled, eBay disabled, Shopify disabled
		settings = self._build_mock_settings(channels=[
			frappe._dict({"channel": "Direct Sales", "send_to_shipstation": 1, "store_id": "4988058"}),
			frappe._dict({"channel": "eBay", "send_to_shipstation": 0, "store_id": ""}),
			frappe._dict({"channel": "Shopify", "send_to_shipstation": 0, "store_id": ""}),
		])

		so_direct = frappe._dict({
			"name": "SO-DIRECT",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"shipstation_order_id": None,
			"shipstation_order_key": None,
		})
		ok, _, is_up = check_so_fulfillment_gate(so_direct, settings=settings)
		self.assertTrue(ok)
		self.assertFalse(is_up)

		so_ebay = frappe._dict({
			"name": "SO-EBAY",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"custom_ebay_order_reference": "EBAY-101",
			"shipstation_order_id": None,
			"shipstation_order_key": None,
		})
		ok, reason, _ = check_so_fulfillment_gate(so_ebay, settings=settings)
		self.assertFalse(ok)
		self.assertIn("eBay channel is not enabled", reason)

		so_shopify = frappe._dict({
			"name": "SO-SHOPIFY",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"custom_shopify_order_reference": "SHOPIFY-202",
			"shipstation_order_id": None,
			"shipstation_order_key": None,
		})
		ok, reason, _ = check_so_fulfillment_gate(so_shopify, settings=settings)
		self.assertFalse(ok)
		self.assertIn("Shopify channel is not enabled", reason)

		# Scenario B: Enable eBay and Shopify in settings
		settings.channels = [
			frappe._dict({"channel": "Direct Sales", "send_to_shipstation": 1, "store_id": "4988058"}),
			frappe._dict({"channel": "eBay", "send_to_shipstation": 1, "store_id": "4988058"}),
			frappe._dict({"channel": "Shopify", "send_to_shipstation": 1, "store_id": "4988058"}),
		]
		ok, _, is_up = check_so_fulfillment_gate(so_ebay, settings=settings)
		self.assertTrue(ok)
		ok, _, is_up = check_so_fulfillment_gate(so_shopify, settings=settings)
		self.assertTrue(ok)

	def test_gate_legacy_inbound_and_update_behavior(self):
		"""Legacy inbound orders (no key) cannot be updated/sent; linked orders with key show update unless active label."""
		settings = self._build_mock_settings(channels=[
			frappe._dict({"channel": "Direct Sales", "send_to_shipstation": 1, "store_id": "4988058"}),
		])
		# Legacy inbound: has shipstation_order_id, but shipstation_order_key is None
		so_legacy = frappe._dict({
			"name": "SO-LEGACY",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"shipstation_order_id": "999888777",
			"shipstation_order_key": None,
		})
		ok, reason, _ = check_so_fulfillment_gate(so_legacy, settings=settings)
		self.assertFalse(ok)
		self.assertIn("legacy inbound order without an order key", reason)

		# Linked outbound order with key and no active label
		so_outbound = frappe._dict({
			"name": "SO-OUTBOUND",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"shipstation_order_id": "999888777",
			"shipstation_order_key": "SO-OUTBOUND",
			"shipments": [],
		})
		ok, _, is_up = check_so_fulfillment_gate(so_outbound, settings=settings)
		self.assertTrue(ok)
		self.assertTrue(is_up)  # Should return is_update = True

		# With active label
		so_outbound.shipments = [
			frappe._dict({"shipment_id": "SHIP-1", "shipment_status": "Shipped", "tracking_number": "1Z123"})
		]
		ok, reason, _ = check_so_fulfillment_gate(so_outbound, settings=settings)
		self.assertFalse(ok)
		self.assertIn("already has an active shipping label", reason)

	# -------------------------------------------------------------
	# 4. Outbound Payload Builder Validation Tests
	# -------------------------------------------------------------

	def test_payload_builder_validations(self):
		"""build_order_payload enforces required items, addresses, and integer quantities."""
		settings = self._build_mock_settings(channels=[
			frappe._dict({"channel": "Direct Sales", "send_to_shipstation": 1, "store_id": "4988058"}),
		])

		# Missing items
		so_no_items = frappe._dict({
			"name": "SO-NO-ITEMS",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"shipstation_order_id": None,
			"shipstation_order_key": None,
			"items": [],
		})
		with self.assertRaises(frappe.ValidationError):
			build_order_payload(so_no_items, settings=settings)

		# Fractional quantity
		so_fractional = frappe._dict({
			"name": "SO-FRACT",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"shipstation_order_id": None,
			"shipstation_order_key": None,
			"customer_name": "Test Customer",
			"customer_address": "ADDR-BILL",
			"shipping_address_name": "ADDR-SHIP",
			"items": [
				frappe._dict({"item_code": "ITEM-1", "name": "row-1", "qty": 1.5, "rate": 10.0, "idx": 1})
			],
		})
		with patch("shipstation_integration.shipstation.orders_outbound.build_address_dict") as mock_addr:
			mock_addr.return_value = {"name": "Test", "street1": "123 Main St", "city": "City", "postalCode": "12345", "country": "US"}
			with self.assertRaises(frappe.ValidationError) as cm:
				build_order_payload(so_fractional, settings=settings)
			self.assertIn("fractional quantity", str(cm.exception).lower())

		# Missing address
		so_no_addr = frappe._dict({
			"name": "SO-NO-ADDR",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
			"shipstation_order_id": None,
			"shipstation_order_key": None,
			"customer_name": "Test Customer",
			"customer_address": None,
			"shipping_address_name": None,
			"items": [
				frappe._dict({"item_code": "ITEM-1", "name": "row-1", "qty": 1.0, "rate": 10.0, "idx": 1})
			],
		})
		with self.assertRaises(frappe.ValidationError):
			build_order_payload(so_no_addr, settings=settings)

	# -------------------------------------------------------------
	# 5. Outbound Send Server Method Execution and Idempotency
	# -------------------------------------------------------------

	@patch("shipstation_integration.shipstation.orders_outbound._post_create_order")
	def test_send_order_to_shipstation_success(self, mock_post):
		"""Successful send stores shipstation_order_id and shipstation_order_key."""
		settings = frappe.get_doc("Shipstation Settings", "Shipstation setup")
		test_so_name = "SAL-ORD-2026-00029"
		so = frappe.get_doc("Sales Order", test_so_name)

		# Ensure mock address resolution and clean state
		mock_post.return_value = frappe._dict({
			"orderId": "482999001",
			"orderKey": test_so_name,
			"orderStatus": "awaiting_shipment",
		})

		with patch("shipstation_integration.shipstation.orders_outbound.build_order_payload") as mock_payload:
			mock_payload.return_value = {
				"orderNumber": test_so_name,
				"orderKey": test_so_name,
				"orderDate": str(so.transaction_date),
				"orderStatus": "awaiting_shipment",
				"items": [],
			}
			result = send_order_to_shipstation(test_so_name)

		self.assertEqual(result["order_id"], "482999001")
		self.assertEqual(result["order_key"], test_so_name)
		self.assertFalse(result["is_update"])

		# Verify written values in DB
		updated_id, updated_key = frappe.db.get_value(
			"Sales Order", test_so_name, ["shipstation_order_id", "shipstation_order_key"]
		)
		self.assertEqual(updated_id, "482999001")
		self.assertEqual(updated_key, test_so_name)

		# Clean up Sales Order fields after test
		frappe.db.set_value("Sales Order", test_so_name, "shipstation_order_id", None)
		frappe.db.set_value("Sales Order", test_so_name, "shipstation_order_key", None)
		frappe.db.commit()

	def test_send_order_to_shipstation_duplicate_rejection(self):
		"""Attempting to create an already-linked order throws ValidationError."""
		test_so_name = "SAL-ORD-2026-00013"  # Already linked with shipstation_order_id
		with self.assertRaises(frappe.ValidationError):
			send_order_to_shipstation(test_so_name)

	def test_missing_shipstation_configuration_error(self):
		"""validate_so_fulfillment_gate throws error if Shipstation Settings are missing."""
		so = frappe._dict({
			"name": "SO-TEST",
			"docstatus": 1,
			"status": "To Deliver and Bill",
			"per_delivered": 0,
		})
		with patch("shipstation_integration.shipstation.orders_outbound.get_active_settings") as mock_set:
			mock_set.return_value = None
			with self.assertRaises(frappe.ValidationError):
				validate_so_fulfillment_gate(so)

	@patch("shipstation_integration.shipstation.orders_outbound._post_create_order")
	def test_send_order_api_failure_and_timeline_comment(self, mock_post):
		"""API failure adds a redacted timeline comment and throws ValidationError."""
		test_so_name = "SAL-ORD-2026-00029"
		so = frappe.get_doc("Sales Order", test_so_name)

		# Clear comments first to count cleanly
		initial_comments = len(frappe.get_all("Comment", filters={"reference_name": test_so_name}))

		class DummyResponse:
			status_code = 400
			def read(self): pass
			def json(self): return {"message": "Invalid store or items Authorization: Bearer secret123"}
			text = "Invalid store or items"

		class DummyHTTPError(Exception):
			response = DummyResponse()

		mock_post.side_effect = DummyHTTPError("API Call Failed")

		with patch("shipstation_integration.shipstation.orders_outbound.build_order_payload") as mock_payload:
			mock_payload.return_value = {
				"orderNumber": test_so_name,
				"orderKey": test_so_name,
				"orderDate": str(so.transaction_date),
				"orderStatus": "awaiting_shipment",
				"items": [],
			}
			with self.assertRaises(frappe.ValidationError):
				send_order_to_shipstation(test_so_name)

		# Verify comment added and credentials redacted
		comments = frappe.get_all(
			"Comment",
			filters={"reference_name": test_so_name},
			fields=["content"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(len(comments) > 0)
		latest_comment = comments[0].content
		self.assertIn("ShipStation send failed", latest_comment)
		self.assertNotIn("secret123", latest_comment)

		# Ensure order was NOT marked as linked
		self.assertIsNone(frappe.db.get_value("Sales Order", test_so_name, "shipstation_order_id"))

	@patch("shipstation_integration.shipstation.orders_outbound.has_active_label", return_value=False)
	def test_pre_update_remote_status_verification(self, mock_active_label):
		"""Updating an order requires remote status to be awaiting_shipment."""
		test_so_name = "SAL-ORD-2026-00029"
		so = frappe.get_doc("Sales Order", test_so_name)
		# Temporarily set linked IDs
		frappe.db.set_value("Sales Order", test_so_name, "shipstation_order_id", "482000000")
		frappe.db.set_value("Sales Order", test_so_name, "shipstation_order_key", test_so_name)
		frappe.db.commit()

		try:
			settings = frappe.get_doc("Shipstation Settings", "Shipstation setup")
			with patch.object(settings, "client") as mock_client_factory:
				mock_client = MagicMock()
				mock_client.get_order.return_value = {"orderStatus": "shipped"}
				mock_client_factory.return_value = mock_client

				with patch("shipstation_integration.shipstation.orders_outbound.get_active_settings", return_value=settings):
					with self.assertRaises(frappe.ValidationError) as cm:
						send_order_to_shipstation(test_so_name)
					self.assertIn("remote status is 'shipped'", str(cm.exception))
		finally:
			frappe.db.set_value("Sales Order", test_so_name, "shipstation_order_id", None)
			frappe.db.set_value("Sales Order", test_so_name, "shipstation_order_key", None)
			frappe.db.commit()
