# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

from datetime import datetime, timedelta, timezone

import frappe
from frappe.utils import get_datetime

from shipstation_integration.shipstation import (
	orders_v1,
	poll_checkpoints,
	shipments_adapter,
	shipments_inbound,
	shipments_v1,
	v1_api,
)

POLL_LOOKBACK_HOURS = 3
POLL_OVERLAP_MINUTES = 60
POLL_MAX_WINDOW_HOURS = 24
POLL_MIN_WINDOW_SECONDS = 300
POLL_LOCK_TTL_SECONDS = 3600
SHIPMENT_DATE_PARAMS = ("createDate", "voidDate")


def to_iso_utc(value: datetime) -> str:
	return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll_window_start(
	checkpoint: datetime | None, now: datetime, since_date: datetime | None = None
) -> datetime:
	if now.tzinfo is None:
		now = now.replace(tzinfo=timezone.utc)
	if since_date and since_date.tzinfo is None:
		since_date = since_date.replace(tzinfo=timezone.utc)

	if not checkpoint:
		return since_date or (now - timedelta(hours=POLL_LOOKBACK_HOURS))

	if checkpoint.tzinfo is None:
		checkpoint = checkpoint.replace(tzinfo=timezone.utc)
	checkpoint_start = checkpoint - timedelta(minutes=POLL_OVERLAP_MINUTES)

	if since_date:
		return max(checkpoint_start, since_date)
	return checkpoint_start


def build_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
	windows = []
	cursor = start
	step = timedelta(hours=POLL_MAX_WINDOW_HOURS)

	while cursor < end:
		window_end = min(cursor + step, end)
		windows.append((cursor, window_end))
		cursor = window_end

	return windows


def _lock_key(store_id: str) -> bytes:
	return frappe.cache().make_key(f"shipstation_poll_store_{store_id}")


def acquire_store_lock(store_id: str) -> bool:
	try:
		return bool(frappe.cache().set(_lock_key(store_id), "1", nx=True, ex=POLL_LOCK_TTL_SECONDS))
	except Exception:
		frappe.log_error(
			title=f"ShipStation shipment poller lock error for store {store_id}",
			message=frappe.get_traceback(),
		)
		return False


def release_store_lock(store_id: str) -> None:
	try:
		frappe.cache().delete(_lock_key(store_id))
	except Exception:
		frappe.log_error(
			title=f"ShipStation shipment poller unlock error for store {store_id}",
			message=frappe.get_traceback(),
		)


def _shipment_window_query(store_id: str, date_param: str, start: datetime, end: datetime) -> dict:
	return {
		"storeId": str(store_id),
		f"{date_param}Start": to_iso_utc(start),
		f"{date_param}End": to_iso_utc(end),
		"includeShipmentItems": "true",
	}


def fetch_shipment_window(
	client, store_id: str, date_param: str, start: datetime, end: datetime
) -> list[dict]:
	query = _shipment_window_query(store_id, date_param, start, end)
	try:
		return v1_api.paginate(
			client, shipments_v1.V1_SHIPMENTS_ENDPOINT, query, "shipments", "shipment"
		)
	except v1_api.ResultLimitExceeded as exc:
		if (end - start).total_seconds() <= POLL_MIN_WINDOW_SECONDS:
			frappe.log_error(
				title=f"ShipStation shipment window too large for store {store_id}",
				message=(
					f"{exc} The window {to_iso_utc(start)} to {to_iso_utc(end)} cannot be split "
					"into smaller windows."
				),
			)
			raise
		middle = start + (end - start) / 2
		return fetch_shipment_window(
			client, store_id, date_param, start, middle
		) + fetch_shipment_window(client, store_id, date_param, middle, end)


def fetch_order_window(client, store_id: str, start: datetime, end: datetime) -> list[dict]:
	query = orders_v1.build_orders_query(store_id, to_iso_utc(start), to_iso_utc(end))
	try:
		return orders_v1.fetch_orders(client, query)
	except v1_api.ResultLimitExceeded as exc:
		if (end - start).total_seconds() <= POLL_MIN_WINDOW_SECONDS:
			frappe.log_error(
				title=f"ShipStation order window too large for store {store_id}",
				message=(
					f"{exc} The window {to_iso_utc(start)} to {to_iso_utc(end)} cannot be split "
					"into smaller windows."
				),
			)
			raise
		middle = start + (end - start) / 2
		return fetch_order_window(client, store_id, start, middle) + fetch_order_window(
			client, store_id, middle, end
		)


def fetch_shipments_of_order(client, store_id: str, order_id: str) -> list[dict]:
	query = {
		"storeId": str(store_id),
		"orderId": str(order_id),
		"includeShipmentItems": "true",
	}
	return v1_api.paginate(
		client, shipments_v1.V1_SHIPMENTS_ENDPOINT, query, "shipments", "shipment"
	)


def enqueue_shipment(norm: dict, source: str = "Poller") -> None:
	shipment_id = norm.get("shipment_id")
	order_id = norm.get("order_id")
	job_identity = shipment_id or f"ORDER-{order_id}"
	voided_flag = 1 if norm.get("voided") else 0
	frappe.enqueue(
		method="shipstation_integration.shipstation.shipments_inbound.process_shipment",
		queue="default",
		job_id=f"process_shipment_{job_identity}_{voided_flag}",
		deduplicate=True,
		shipment=norm,
		source=source,
	)


def poll_store_shipments(
	client,
	store_id: str,
	now: datetime,
	settings_name: str | None = None,
	since_date: datetime | None = None,
	synchronous: bool = False,
):
	checkpoint = poll_checkpoints.read_checkpoint(
		settings_name, store_id, poll_checkpoints.SHIPMENT_PIPELINE
	)
	start = poll_window_start(checkpoint, now, since_date=since_date)
	seen = set()
	fetched = 0
	processed = 0

	for window_start, window_end in build_windows(start, now):
		for date_param in SHIPMENT_DATE_PARAMS:
			shipments = fetch_shipment_window(client, store_id, date_param, window_start, window_end)
			fetched += len(shipments)
			for shipment in shipments:
				norm = shipments_adapter.from_api_dict(shipment)
				shipment_id = norm.get("shipment_id")
				if not shipment_id:
					frappe.log_error(
						title=f"ShipStation shipment without shipment id for store {store_id}",
						message=(
							"A shipment returned by the V1 API has no shipment id and cannot be "
							"identified, so it was not processed."
						),
					)
					continue

				voided_flag = 1 if norm.get("voided") else 0
				seen_key = (shipment_id, voided_flag)
				if seen_key in seen:
					continue
				seen.add(seen_key)

				if synchronous:
					shipments_inbound.process_shipment(norm, source="Poller", synchronous=synchronous)
				else:
					enqueue_shipment(norm)
				processed += 1

		poll_checkpoints.advance_checkpoint(
			settings_name, store_id, poll_checkpoints.SHIPMENT_PIPELINE, window_end
		)
	return fetched, processed, start


def poll_store_shipped_orders(
	client,
	store_id: str,
	now: datetime,
	settings_name: str | None = None,
	since_date: datetime | None = None,
	synchronous: bool = False,
):
	checkpoint = poll_checkpoints.read_checkpoint(
		settings_name, store_id, poll_checkpoints.ORDER_PIPELINE
	)
	start = poll_window_start(checkpoint, now, since_date=since_date)
	fetched = 0
	processed = 0

	for window_start, window_end in build_windows(start, now):
		for order in fetch_order_window(client, store_id, window_start, window_end):
			norm = orders_v1.from_api_dict(order)
			if norm.get("order_status") != orders_v1.V1_ORDER_STATUS_SHIPPED:
				continue

			fetched += 1
			order_id = norm.get("order_id")
			if not order_id:
				continue

			order_number = norm.get("order_number")
			sales_orders = shipments_inbound.find_sales_orders(order_number)
			if len(sales_orders) != 1:
				shipments_inbound.upsert_exception(
					None,
					order_number,
					"Multiple Match" if sales_orders else "No Match",
					",".join(sales_orders),
					"Poller",
					order_id=order_id,
				)
				continue

			so = frappe.get_doc("Sales Order", sales_orders[0])
			if shipments_inbound.has_active_label(so, order_id):
				reconcile_external_rows(so, order_id, synchronous=synchronous)
				continue

			labelled = fetch_shipments_of_order(client, store_id, order_id)
			if labelled:
				for shipment in labelled:
					shipment_norm = shipments_adapter.from_api_dict(shipment)
					if shipment_norm.get("voided"):
						continue
					if synchronous:
						shipments_inbound.process_shipment(shipment_norm, source="Poller", synchronous=synchronous)
					else:
						enqueue_shipment(shipment_norm)
					processed += 1
				continue

			if synchronous:
				shipments_inbound.process_shipment(norm, source="Poller", synchronous=synchronous)
			else:
				enqueue_shipment(norm)
			processed += 1

		poll_checkpoints.advance_checkpoint(
			settings_name, store_id, poll_checkpoints.ORDER_PIPELINE, window_end
		)
	return fetched, processed, start


def reconcile_external_rows(so, order_id: str, synchronous: bool = False) -> None:
	removed = shipments_inbound.remove_external_rows(so, order_id)
	if not removed:
		return
	shipments_inbound.sync_sales_order_header(so)
	for attempt in range(3):
		try:
			so.save(ignore_permissions=True)
			return
		except frappe.TimestampMismatchError:
			if not synchronous:
				raise frappe.RetryBackgroundJobError
			if attempt == 2:
				raise
			so.reload()
			if not shipments_inbound.remove_external_rows(so, order_id):
				return
			shipments_inbound.sync_sales_order_header(so)


def poll_settings_shipments(sss_doc, synchronous: bool = False):
	client = sss_doc.client()
	now = datetime.now(timezone.utc)
	settings_name = getattr(sss_doc, "name", None)
	since_date = getattr(sss_doc, "since_date", None)
	if since_date:
		since_date = get_datetime(since_date)
		if since_date.tzinfo is None:
			since_date = since_date.replace(tzinfo=timezone.utc)

	shipments_fetched = 0
	shipments_processed = 0
	orders_fetched = 0
	orders_processed = 0
	stores_attempted = 0
	stores_succeeded = 0
	earliest_start = None
	errors = []

	for store in (sss_doc.shipstation_stores or []):
		store_id = getattr(store, "store_id", None)
		if not store_id or not getattr(store, "track_shipments", 1):
			continue

		store_id = str(store_id)
		stores_attempted += 1

		if not acquire_store_lock(store_id):
			msg = f"Store {store_id} polling lock is active"
			frappe.log_error(
				title=f"ShipStation shipment poller skipped store {store_id}",
				message="A poll for this store is still running, so this run was skipped.",
			)
			errors.append(msg)
			continue

		store_failed = False
		try:
			try:
				f, p, s_start = poll_store_shipments(
					client, store_id, now, settings_name, since_date=since_date, synchronous=synchronous
				)
				shipments_fetched += f
				shipments_processed += p
				if earliest_start is None or s_start < earliest_start:
					earliest_start = s_start
			except Exception as e:
				store_failed = True
				frappe.db.rollback()
				frappe.log_error(
					title=f"ShipStation shipment poller error for store {store_id}",
					message=frappe.get_traceback(),
				)
				errors.append(f"Store {store_id} shipments: {e}")

			try:
				f, p, o_start = poll_store_shipped_orders(
					client, store_id, now, settings_name, since_date=since_date, synchronous=synchronous
				)
				orders_fetched += f
				orders_processed += p
				if earliest_start is None or o_start < earliest_start:
					earliest_start = o_start
			except Exception as e:
				store_failed = True
				frappe.db.rollback()
				frappe.log_error(
					title=f"ShipStation order poller error for store {store_id}",
					message=frappe.get_traceback(),
				)
				errors.append(f"Store {store_id} shipped orders: {e}")
		finally:
			release_store_lock(store_id)

		if not store_failed:
			stores_succeeded += 1

	window_start = earliest_start or since_date or (now - timedelta(hours=POLL_LOOKBACK_HOURS))
	return {
		"window_start": window_start.strftime("%Y-%m-%d %H:%M:%S UTC"),
		"window_end": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
		"fetched": shipments_fetched + orders_fetched,
		"processed": shipments_processed + orders_processed,
		"shipments_fetched": shipments_fetched,
		"shipments_processed": shipments_processed,
		"orders_fetched": orders_fetched,
		"orders_processed": orders_processed,
		"stores_attempted": stores_attempted,
		"stores_succeeded": stores_succeeded,
		"errors": errors,
	}


def poll_shipments() -> None:
	settings_names = frappe.get_all("Shipstation Settings", filters={"enabled": 1}, pluck="name")
	for name in settings_names:
		try:
			sss_doc = frappe.get_doc("Shipstation Settings", name)
			if not sss_doc.enabled or not sss_doc.enable_legacy_api:
				continue
			poll_settings_shipments(sss_doc)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"ShipStation shipment poller error for {name}",
				message=frappe.get_traceback(),
			)
