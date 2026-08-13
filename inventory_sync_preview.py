"""Create and store one maintenance-mode inventory mirror preview."""

import json

from sqlalchemy.orm import Session

from database import engine, upgrade_existing_database
from inventory_sync_workflow import create_inventory_sync_preview
from models import InventorySyncJob


def main():
    upgrade_existing_database()
    preview = create_inventory_sync_preview()
    with Session(engine) as session:
        job = InventorySyncJob(
            status="completed",
            mode="maintenance_preview",
            snapshot_json=json.dumps(preview, default=str),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        print(json.dumps({
            "job_id": job.id,
            "preview_timestamp": preview["preview_timestamp"],
            "summary": preview["summary"],
            "order_ingestion": preview["order_ingestion"],
        }, indent=2))


if __name__ == "__main__":
    main()
