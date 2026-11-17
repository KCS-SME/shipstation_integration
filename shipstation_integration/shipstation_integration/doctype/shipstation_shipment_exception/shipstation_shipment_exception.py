import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint
from shipstation_integration.shipstation import shipments_adapter


class ShipStationShipmentException(Document):
	@frappe.whitelist()
	def retry(self):
		try:
			if self.status != "Open":
				frappe.throw(_("Can only retry Open exceptions"))

			user_roles = frappe.get_roles()
			has_role = "System Manager" in user_roles or "Stock Manager" in user_roles
			if not (frappe.has_permission(self.doctype, "write") and has_role):
				frappe.throw(_("Not permitted to retry exceptions"), frappe.PermissionError)

			self.retry_count = cint(self.retry_count or 0) + 1
			self.db_set("retry_count", self.retry_count, update_modified=False)

			names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
			if not names:
				frappe.throw(_("No enabled Shipstation Settings record found"))
			sss_doc = frappe.get_doc("Shipstation Settings", names[0])

			if not self.order_id and not self.shipment_id:
				frappe.throw(_("Cannot retry exception without shipment_id or order_id"))

			client = sss_doc.client()
			is_external = not self.shipment_id or str(self.shipment_id).startswith("EXT-") or str(self.shipment_id).startswith("ORDER-")

			if is_external:
				if not self.order_id:
					frappe.throw(_("Cannot retry external shipment exception without order_id"))
				order_obj = client.get_order(self.order_id)
				order_status = getattr(order_obj, "order_status", None) if not isinstance(order_obj, dict) else order_obj.get("orderStatus")
				if order_status != "shipped":
					frappe.throw(_("Order {0} status in ShipStation is '{1}', expected 'shipped'").format(self.order_id, order_status))
				from shipstation_integration.shipstation import orders_v1
				shipment_to_process = orders_v1.from_api_dict(order_obj)
				job_id = f"process_shipment_EXT-{self.order_id}_0"
			else:
				if not self.order_id:
					frappe.throw(_("Cannot determine order_id for shipment retry"))
				order_obj = client.get_order(self.order_id)
				store_id = getattr(getattr(order_obj, "advanced_options", None), "store_id", None) if not isinstance(order_obj, dict) else order_obj.get("advancedOptions", {}).get("storeId")
				if not store_id:
					frappe.throw(_("Cannot determine store_id from order"))

				from shipstation_integration.shipstation.shipments_poller import fetch_shipments_of_order
				shipments = fetch_shipments_of_order(client, str(store_id), str(self.order_id))
				shipment_to_process = None
				for s in shipments:
					if str(s.get("shipmentId")) == str(self.shipment_id):
						if s.get("voided"):
							frappe.throw(_("Shipment is voided in ShipStation; cannot retry."))
						shipment_to_process = shipments_adapter.from_api_dict(s)
						break

				if not shipment_to_process:
					frappe.throw(_("Shipment {0} not found in ShipStation for order {1}").format(self.shipment_id, self.order_id))

				job_id = f"process_shipment_{self.shipment_id}_0"

			job = frappe.enqueue(
				method="shipstation_integration.shipstation.shipments_inbound.process_shipment",
				queue="default",
				job_id=job_id,
				deduplicate=True,
				shipment=shipment_to_process,
				source="Retry",
			)
			if job:
				frappe.msgprint(_("Shipment enqueued for retry processing."))
			else:
				frappe.msgprint(_("Shipment processing is already enqueued."))
		except Exception as e:
			self.db_set({"status": "Open", "error_message": str(e)}, update_modified=False)
			frappe.db.commit()
			raise
