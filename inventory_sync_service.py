"""Database-backed serialization for inventory-affecting operations."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import engine
from models import CleanRebuildExecution, FloorCorrectionExecution, InventorySyncLease


LEASE_NAME = "inventory"


class InventoryLeaseBusy(RuntimeError):
    pass


def acquire_inventory_lease(session: Session, owner_token: str, ttl_seconds=300,
                            allow_active_cutover=False):
    now = datetime.now()
    if not allow_active_cutover and session.query(CleanRebuildExecution).filter(
        CleanRebuildExecution.status.in_({
            "prepared", "blanking", "blank_verification", "republishing",
            "final_reconciliation", "recovery_required",
        }),
    ).first():
        raise InventoryLeaseBusy(
            "Inventory changes are blocked by an active/recovery-required clean rebuild."
        )
    if not allow_active_cutover and session.query(FloorCorrectionExecution).filter(
        FloorCorrectionExecution.status.in_({"prepared", "applying", "verifying", "recovery_required"}),
    ).first():
        raise InventoryLeaseBusy(
            "Inventory changes are blocked by an active/recovery-required floor correction."
        )
    current = session.get(InventorySyncLease, LEASE_NAME)
    if current and current.expires_at > now:
        raise InventoryLeaseBusy("Another inventory operation is already running.")
    if current:
        session.delete(current)
        session.flush()
    session.add(InventorySyncLease(
        name=LEASE_NAME,
        owner_token=owner_token,
        acquired_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    ))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise InventoryLeaseBusy("Another inventory operation acquired the lease.") from exc


def release_inventory_lease(session: Session, owner_token: str):
    lease = session.get(InventorySyncLease, LEASE_NAME)
    if lease and lease.owner_token == owner_token:
        session.delete(lease)
        session.commit()


@contextmanager
def inventory_sync_lease(ttl_seconds=300, allow_active_cutover=False):
    owner_token = str(uuid4())
    with Session(engine) as session:
        acquire_inventory_lease(
            session, owner_token, ttl_seconds=ttl_seconds,
            allow_active_cutover=allow_active_cutover,
        )
    try:
        yield owner_token
    finally:
        with Session(engine) as session:
            release_inventory_lease(session, owner_token)


def inventory_locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with inventory_sync_lease():
            return function(*args, **kwargs)
    return wrapped
