# Producer-derived review-candidate golden

`source.json` is a deterministic normalized retry/failure artifact whose endpoint
is the qualified tool key `syncmill::board_stats`. Running

```bash
tracegraph export-review-candidates tool-retry-failure source.json --out v1.json
```

must reproduce `v1.json` byte-for-byte. The candidate digest is the SHA-256 of
the exact `source.json` bytes, so consumers can vendor an honest producer output
rather than a schema-only example.
