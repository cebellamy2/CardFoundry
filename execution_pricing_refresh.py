"""Create a non-destructive execution-pricing seal for an approved preview."""
import argparse
import json

from clean_rebuild_workflow import refresh_production_execution_pricing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("preview_job_id", type=int)
    args = parser.parse_args()
    print(json.dumps(refresh_production_execution_pricing(args.preview_job_id), indent=2,
                     sort_keys=True))


if __name__ == "__main__":
    main()
