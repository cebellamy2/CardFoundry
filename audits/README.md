# Production Audit Policy

Files named `production-*.json` are immutable, sanitized records of completed
production imports and cutover snapshots. Keep them in this directory and
commit them to Git after reviewing that they contain no credentials, access
tokens, customer/order data, or source CSV contents.

Audit files may contain source filenames and SHA-256 hashes, CardFoundry batch,
import, and inventory identifiers, validation evidence, counts, and timestamps.
Do not edit a historical audit. If a correction is required, create a new
timestamped audit that references the superseded record.

Diagnostic logs that contain API payloads, marketplace responses, credentials,
or customer/order information do not belong here and must not be committed.
