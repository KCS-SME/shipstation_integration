import frappe


def execute():
	notification_name = "Insufficient Stock Shipment Exception"
	role = "Stock Manager"
	settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
	if settings_names:
		settings_role = frappe.db.get_value("Shipstation Settings", settings_names[0], "notification_role")
		if settings_role:
			role = settings_role

	if not frappe.db.exists("Notification", notification_name):
		doc = frappe.new_doc("Notification")
		doc.name = notification_name
		doc.subject = "Insufficient Stock: {{ doc.shipment_id or doc.order_id }}"
		doc.document_type = "ShipStation Shipment Exception"
		doc.event = "Custom"
		doc.channel = "Email"
		doc.condition = "doc.reason == 'Insufficient Stock' and doc.status == 'Open'"
		doc.message = (
			"<p>Shipment <b>{{ doc.shipment_id or doc.order_id }}</b> failed due to insufficient stock.</p>"
			"<p><b>Sales Order:</b> {{ doc.sales_order or 'N/A' }}<br>"
			"<b>Error:</b> {{ doc.error_message or 'N/A' }}</p>"
		)
		doc.append("recipients", {"receiver_by_role": role})
		doc.insert(ignore_permissions=True)
	else:
		doc = frappe.get_doc("Notification", notification_name)
		doc.event = "Custom"
		doc.condition = "doc.reason == 'Insufficient Stock' and doc.status == 'Open'"
		if not doc.recipients:
			doc.append("recipients", {"receiver_by_role": role})
		else:
			doc.recipients[0].receiver_by_role = role
		doc.save(ignore_permissions=True)
