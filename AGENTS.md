# Project development notes

- This is the independent Python control plane. Do not modify the sibling Pi checkout.
- Keep runtime dependencies in the Python standard library for the current release.
- Run `python -m unittest discover -s tests -v` after code changes.
- Tests use protocol fixtures and offline replay, never real model credentials.
- Distinguish deterministic replay, protocol integration and real model experiments in reports.
- Recursion is required: an accepted improvement skill must generate later changes, including a successor of itself. Preserve parent/generator/evidence lineage.
- Ownership and scope checks, regression cases and promotion gates are outside the generated patch vocabulary.
- Preserve scoped memory, versioned components and bounded recursive improvement. Fixed contract gates are not proof of coding-quality gains; arbitrary tool-code evolution is unsupported.
- Never commit credentials, runtime databases or private trajectories.
