# CardFoundry

CardFoundry is a production inventory and fulfillment application for a small
Magic: The Gathering singles store. CardFoundry is the authoritative record of
physical inventory; Mana Pool is the marketplace and publication target.

The production store went live on 2026-08-13 with 6,864 sellable cards across
4,888 Mana Pool variants. Quantity reconciliation passed exactly, and every
positive listing was at or above the configured $0.65 floor.

## Capabilities

- Reviewed, atomic CSV batch imports with exact physical-quantity expansion
- Canonical printing and language validation with guarded remote bindings
- Inventory search, printing correction, and immutable batch provenance
- Available, Not For Sale, reserved, sold, and removed inventory lifecycles
- Local dispositions and audited inventory corrections
- Order ingestion, allocation, and pick-wave workflows
- Competitive, market-fallback, and reviewed manual initial pricing
- Store-off inventory rebuild previews with sealed prices and resumable recovery
- Durable production audit artifacts

## Development quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. Configure credentials in an untracked `.env`;
never commit tokens or `cardfoundry.db`.

## Documentation

- [Production go-live](docs/PRODUCTION_GO_LIVE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operator user manual](docs/USER_MANUAL.md)
- [Operations runbook](docs/OPERATIONS_RUNBOOK.md)
- [Development guide](docs/DEVELOPMENT.md)
- [Future roadmap](docs/ROADMAP.md)

> **Production safety:** Preview and local inventory actions are distinct from
> Mana Pool writes. Do not run a marketplace-writing workflow unless its store
> state, snapshot, typed confirmation, journal, and recovery requirements have
> been reviewed for that exact operation.
