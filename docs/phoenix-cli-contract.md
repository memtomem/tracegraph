# Phoenix runtime pin maintenance

Tracegraph keeps its tested Phoenix CLI and server versions in
[`contracts/phoenix-runtime.json`](../contracts/phoenix-runtime.json). The CLI runtime floor in
the application is a separate compatibility promise and must not be raised merely because CI
tests a newer release.

## Updating the CLI pin

1. Change `phoenix_cli` in the manifest to one exact published version.
2. Run the contract test with that version:

   ```bash
   PX_VERSION=$(jq -r .phoenix_cli contracts/phoenix-runtime.json)
   python scripts/verify_phoenix_cli_contract.py npx --yes @arizeai/phoenix-cli@$PX_VERSION
   ```

3. Run `uv run pytest tests/test_phoenix_cli_contract.py tests/test_phoenix_adapter.py`.
4. Confirm the `Phoenix CLI contract` CI job passes before merging.

The contract test deliberately reports launcher stderr but never echoes launcher stdout, which
may contain exported trace data.

## Updating the Phoenix server pin

1. Select an exact release tag and resolve its immutable Linux digest.
2. Update `version` and `digest` together. The E2E workflow runs
   `image:version@digest`, so a moved tag cannot silently change the tested server.
3. Run the manual `Phoenix + SyncMill E2E` workflow and verify both diagnosis and review-candidate
   artifacts before merging.

Scheduled E2E runs use SyncMill `main`; manual runs may select a commit or tag. Each run records
the resolved SyncMill commit SHA for reproducibility.
