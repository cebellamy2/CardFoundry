# Roadmap

Everything in this document is **FUTURE / NOT YET IMPLEMENTED**.

## Branded order and pull sheets

- Add store logo and branding.
- Batch-print documents after cards are pulled from a pick wave.
- Produce a Mana Pool-like packing/order document.
- Design a fold that works cleanly with a windowed envelope.

## Personal-use and deck management

- Build on `status=unsellable`, `reason=personal_use`, and existing notes.
- Add easier deck/location association and movement history.
- Consider dedicated deck tracking without destroying original batch provenance.

## Multi-store productization

- Isolate configuration per store or tenant.
- Add guided onboarding and store branding.
- Improve credentials/secrets management.
- Enforce tenant/store data separation.
- Package deployment and safer initial setup.
- Create documentation appropriate for other small TCG sellers.

## Sealed-product inventory

The *Warhammer 40,000 Commander Deck: Forces of the Imperium* was intentionally
excluded from the initial singles inventory as `excluded_sealed_product`.
Future sealed inventory should use a separate model and workflow. Do not force
sealed products into `InventoryCard` or invent singles identity metadata.
