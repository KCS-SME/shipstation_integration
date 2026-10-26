# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from shipstation_integration.shipstation.poll_checkpoints import checkpoint_name


class ShipstationPollCheckpoint(Document):
	"""Durable polling progress of one ShipStation store.

	The name of the record is derived from the settings record and the
	ShipStation store id, so a store always maps onto exactly one checkpoint
	record and two workers cannot create two checkpoints for the same store.
	"""

	def autoname(self):
		self.name = checkpoint_name(self.settings, self.store_id)
