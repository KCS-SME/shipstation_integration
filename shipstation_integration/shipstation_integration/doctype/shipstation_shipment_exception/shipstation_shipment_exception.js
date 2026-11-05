frappe.ui.form.on("ShipStation Shipment Exception", {
	refresh(frm) {
		const can_retry = Boolean(frm.doc.shipment_id || frm.doc.order_id);
		const has_role = frappe.user_roles.includes("System Manager") || frappe.user_roles.includes("Stock Manager");
		if (frm.doc.status === "Open" && !frm.is_new() && can_retry && has_role) {
			frm.add_custom_button(__("Retry"), () => {
				frm.call({
					doc: frm.doc,
					method: "retry",
					freeze: true,
					freeze_message: __("Re-fetching and queuing shipment..."),
					callback() {
						frm.reload_doc();
					}
				});
			});
		}
	}
});
