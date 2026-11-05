// Copyright (c) 2026, AgriTheory and contributors
// For license information, please see license.txt

frappe.ui.form.on('Sales Order', {
	refresh: frm => {
		if (window.shipping && typeof shipping.shipstation === 'function') {
			try {
				shipping.shipstation(frm)
			} catch (e) {
				console.error('Error initializing ShipStation shipping:', e)
			}
		}

		frm.page.remove_inner_button(__('Send Order to ShipStation'))
		frm.page.remove_inner_button(__('Update in ShipStation'))
		frm.page.remove_inner_button(__('Send to ShipStation'))

		if (frm.doc.docstatus === 1) {
			frappe.call({
				method: 'shipstation_integration.shipstation.orders_outbound.get_shipstation_send_action',
				args: { sales_order: frm.doc.name },
				callback: r => {
					frm.page.remove_inner_button(__('Send Order to ShipStation'))
					frm.page.remove_inner_button(__('Update in ShipStation'))
					frm.page.remove_inner_button(__('Send to ShipStation'))

					if (!r.message || r.message === 'none') {
						return
					}
					const label = r.message === 'update' ? __('Update in ShipStation') : __('Send Order to ShipStation')
					frm.add_custom_button(label, () => {
						frappe.call({
							method: 'shipstation_integration.shipstation.orders_outbound.send_order_to_shipstation',
							args: { sales_order: frm.doc.name },
							freeze: true,
							freeze_message: __('Processing ShipStation Order...'),
							callback: res => {
								if (!res.exc) {
									frm.reload_doc()
								}
							},
						})
					})
				},
			})
		}
	},
})

