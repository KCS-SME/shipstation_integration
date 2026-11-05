# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

from typing import NoReturn

import frappe

V1_MAX_PAGE_SIZE = 500
V1_MAX_RESULTS = 10000
V1_MAX_PAGES = V1_MAX_RESULTS // V1_MAX_PAGE_SIZE


class ResultLimitExceeded(Exception):
	pass


def paginate(client, endpoint: str, query: dict, items_key: str, subject: str) -> list[dict]:
	items: list[dict] = []
	page = 1

	while True:
		payload = {**query, "page": str(page), "pageSize": str(V1_MAX_PAGE_SIZE)}
		response = client.get(endpoint=endpoint, payload=payload)
		response.raise_for_status()

		data = response.json()
		page_items = data.get(items_key) or []
		total, pages = _pagination_metadata(data, page, len(page_items), subject)

		if total is not None and total > V1_MAX_RESULTS:
			raise ResultLimitExceeded(
				f"ShipStation reported {total} {subject}s for this query; at most "
				f"{V1_MAX_RESULTS} records can be retrieved."
			)
		if pages > V1_MAX_PAGES:
			raise ResultLimitExceeded(
				f"ShipStation reported {pages} pages for this query; at most "
				f"{V1_MAX_PAGES} pages of {V1_MAX_PAGE_SIZE} records can be retrieved."
			)

		items.extend(page_items)

		if page >= pages:
			break
		if not page_items:
			_fail_lookup(
				f"ShipStation reported {pages} pages but returned no {subject}s on page {page}.",
				subject,
			)
		page += 1

	return items


def _pagination_metadata(
	data: dict, requested_page: int, item_count: int, subject: str
) -> tuple[int | None, int]:
	response_page = _metadata_int(data, "page", subject)
	pages = _metadata_int(data, "pages", subject)
	total = _metadata_int(data, "total", subject, required=False)

	if response_page != requested_page:
		_fail_lookup(
			f"ShipStation returned page {response_page} while page {requested_page} was requested.",
			subject,
		)
	if pages < 1:
		if item_count:
			_fail_lookup(f"ShipStation reported no pages but returned {subject}s.", subject)
		return total, 0

	return total, pages


def _metadata_int(data: dict, key: str, subject: str, required: bool = True) -> int | None:
	value = data.get(key)
	if value is None:
		if required:
			_fail_lookup(f"ShipStation response is missing the '{key}' pagination field.", subject)
		return None
	try:
		return int(value)
	except (TypeError, ValueError):
		_fail_lookup(
			f"ShipStation response has an invalid '{key}' pagination value: {value!r}.", subject
		)


def _fail_lookup(message: str, subject: str) -> NoReturn:
	frappe.log_error(title=f"ShipStation V1 {subject} lookup failed", message=message)
	frappe.throw(message)
