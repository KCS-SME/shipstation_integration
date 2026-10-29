# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

__version__ = "16.0.1"


def _patch_shipstation_client():
	"""
	Patch ShipStationBase._structure to allow arbitrary values for Literal types (such as
	billToParty: 4 from ShipStation API) without throwing cattrs ClassValidationError.
	This avoids needing changes to the upstream shipstation-client package.
	"""
	try:
		from datetime import date, datetime
		from decimal import Decimal
		from typing import Literal, get_origin
		from uuid import UUID
		from cattrs import Converter
		from dateutil.parser import parse
		from shipstation.base import ShipStationBase

		def _patched_structure(self, json_input):
			conv = Converter()
			conv.register_structure_hook(Decimal, lambda d, t: Decimal(d))
			conv.register_structure_hook(datetime, lambda dt, t: parse(dt))
			conv.register_structure_hook(date, lambda dt, t: parse(dt))
			conv.register_structure_hook(UUID, lambda d, t: UUID(d))
			conv.register_structure_hook_func(lambda t: get_origin(t) is Literal, lambda val, t: val)
			return conv.structure(json_input, type(self))

		ShipStationBase._structure = _patched_structure
	except Exception:
		pass


_patch_shipstation_client()

