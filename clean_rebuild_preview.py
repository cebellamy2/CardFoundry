"""Generate and persist a read-only clean-rebuild preview."""

import json
from sqlalchemy.orm import Session

from clean_rebuild_workflow import create_clean_rebuild_preview
from database import engine
from models import InventorySyncJob


def main():
    preview = create_clean_rebuild_preview()
    with Session(engine) as session:
        job = InventorySyncJob(
            status="completed", mode="clean_rebuild_preview",
            snapshot_json=json.dumps(preview, sort_keys=True),
        )
        session.add(job); session.commit(); job_id = job.id
    print(json.dumps({
        "job_id": job_id, "preview_timestamp": preview["preview_timestamp"],
        "summary": preview["summary"],
        "initial_pricing_summary": preview["initial_pricing_summary"],
        "order_ingestion": preview["order_ingestion"],
    }, indent=2))


if __name__ == "__main__":
    main()
