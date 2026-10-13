# Swiss crop importer

The original reconstruction has no project manifest. The importer in
`src/railway_bim/pilot.py` recognizes its recorded SHA-256 fingerprint and loads
`pilot_contract.json`. That file contains this crop's two rail pairs, terrain
partition rules, and sampling settings. It gives the pilot a persistent project
UUID while retaining its unresolved survey references.

This adapter does not infer topology for another scan. Add `project.json` beside
that reconstruction's `work` folder. Use the shared contract in
`contracts/project.schema.json` and declare its actual tracks and reference
evidence. The writer then uses those track associations rather than rail-list
positions.

The first exported IFC and its version-one sidecars are retained unchanged.
Edits to those deliveries preserve their original identities. New exports use
project identity and stable asset IDs, so another observation of the same
project can retain its asset GUIDs.
