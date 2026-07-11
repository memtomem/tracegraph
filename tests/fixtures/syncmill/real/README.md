# Real SyncMill compete fixture

`compete.otlp.json` was exported by Toolgraph's `scripts/ecosystem_smoke.py`
from fetched, detached source revisions:

- toolgraph: `db7e3293713980a133f97f886ca614a0f901d9df`
- syncmill: `d2344ca590ea093b8aaf1317934a439db1d5820e`
- tracegraph: `8a6a7bc9b1fdb400b5e8c94b5924b33a3af2a5c9`

SHA-256: `30a0069caa5f26489746158e5234b4a20e827345115a5299809bb6568855a626`

The smoke used deterministic local Codex/Kimi stubs; it made no model calls.
The fixture is body-free: it contains structural span metadata, bounded status
fields, IDs, timestamps, counts, and digests only. Prompts, agent output,
patches, worktrees, executable stubs, and the disposable repository were not
retained.
