# Repository split decision and verification

## Decision

The combined compiler was split at common commit `4d7474b` into two self-contained public repositories.

- `Qh-Magic/ossie-starrocks` owns the `ossie_starrocks` package, `ossie-starrocks` command, StarRocks renderer, and `live-starrocks` workflow.
- `Qh-Magic/ossie-trino` owns the `ossie_trino` package, `ossie-trino` and `ossie-gateway` commands, Trino protocol client, gateway, examples, and `live-trino` workflow.

Both public `Compiler.compile(query)` APIs are single-target and do not accept a `target` parameter. Each repository contains its own copy of the target-neutral semantic core so it can install, test, and publish without a runtime dependency on the other project. A third public core package was rejected because the requested result was exactly two independently operable projects.

The Trino compiler retains the proven two-stage renderer but renames its private StarRocks-shaped base from `_starrocks.py` to `_portable_sql.py`. This was a mechanical ownership rename, not a renderer rewrite.

## Persistent gates

Each repository runs `scripts/verify_repository_identity.py` from `scripts/verify.sh`. The gate checks the distribution name, console scripts, single-target compiler signature, required renderer modules, and absence of the opposite live workflow and runtime modules.

Both repositories commit the same `shared-core-files.txt` manifest and `scripts/compare_shared_core.py`. The comparison normalizes only the package name used by `_schema.py`; every other difference in the target-neutral core fails. This cross-repository gate is intentionally manual because each project must remain independently testable without checking out the other repository.

## Verified artifacts

### Trino

- Split commit: [`28ba14c`](https://github.com/Qh-Magic/ossie-trino/commit/28ba14c6e5a7e5ef87b1d2727c96b8c6b3854224)
- Python 3.11, 3.12, and 3.13 matrix plus wheel: [run 43967766442](https://github.com/Qh-Magic/ossie-trino/actions/runs/43967766442)
- Trino 476 differential, protocol, CLI, timeout, result cap, audit, and authenticated gateway: [run 43967775221](https://github.com/Qh-Magic/ossie-trino/actions/runs/43967775221)

### StarRocks

- Split commit: [`b4a9ec6`](https://github.com/Qh-Magic/ossie-starrocks/commit/b4a9ec6687db8993d7c6adfc0596efe249cba531)
- Python 3.11, 3.12, and 3.13 matrix plus wheel: [run 43968094622](https://github.com/Qh-Magic/ossie-starrocks/actions/runs/43968094622)
- StarRocks 3.5 differential: [run 43968104374](https://github.com/Qh-Magic/ossie-starrocks/actions/runs/43968104374)

### Cross-repository

The final local verification built both wheels, installed them into one clean target directory, imported both namespaces, and compiled the same model. The StarRocks package produced backtick-quoted SQL and the Trino package produced double-quoted SQL. `scripts/compare_shared_core.py` also reported that the manifested semantic core matched.
