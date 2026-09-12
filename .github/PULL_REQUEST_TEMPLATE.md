## Summary

<!-- What changes, and why. One or two sentences is usually enough. -->

<!-- If this closes an issue, say so: Closes #N -->

## Validation

<!--
Say what you actually ran and what it printed. A reviewer should not have to guess which
parts you exercised locally.

    uv lock --check
    uv sync --locked --extra cypher
    uvx ruff@0.14.2 check src tests scripts examples
    uv run --no-sync --extra cypher pytest -q

CI covers more than this: the installed-wheel smoke builds the wheel, installs it into a
fresh locked environment, and runs scripts/verify_wheel.py from outside the checkout — an
import that only works from the repo root fails there. Say what you could not check rather
than implying the commands above are equivalent.
-->

## Notes for the reviewer

<!--
Optional. Useful things to put here:
- a behavior change, and what depends on the old behavior
- a decision that could reasonably have gone the other way
- what you deliberately left out of scope
-->

---

<!--
Before you open this:

- **Does it claim something new about a run?** This project's product is a claim about
  causality. A new causal edge needs something the source system *declared*; a new name needs
  evidence that identifies it. "No observed error" is never "the run succeeded". See
  CONTRIBUTING.md → "What this project treats as a defect".
- **Contract or schema change?** `contracts/` is consumed by other repositories, the
  review-candidate golden fixture is compared byte for byte, and the artifact envelope sits
  inside the digest consumers bind to. A test that only reads a fixture does not catch
  producer drift — drive the producer.
- **Adapter change?** Pin it against a real run through a real saver. A synthetic fixture
  cannot show a reconstruction is correct, because getting it wrong is what produces a
  plausible-looking fixture.
- **First contribution?** A bot will ask you to sign the CLA on this pull request. See
  CONTRIBUTING.md.
- **Release?** README and CHANGELOG must be final *in* the tagged commit — pyproject embeds
  the README as package metadata, so it cannot be fixed after the fact.
-->
