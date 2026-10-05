# Repository Guidelines

## Project Structure & Module Organization

Source code lives in `src/qwork/`. Each native Nautilus Actor has its own directory and README under `actors/`; the broken-wing strategy is under `strategies/`. `analytics/` contains financial calculations, `providers/` handles market data, `core/` holds configuration and shared state, and `runtime/` assembles native nodes. `backtesting/` owns replay and reporting; `ui/` contains Dash, Plotly and pywebview. Tests belong in `tests/`, documentation in `docs/`, and generated data in ignored `data/`.

## Build, Test, and Development Commands

- `./scripts/setup.ps1`: create the environment and install the editable package and development dependencies.
- `./scripts/start.ps1`: open the desktop dashboard.
- `.venv/Scripts/python.exe -m qwork dashboard --no-gui`: serve the local Dash interface.
- `.venv/Scripts/python.exe -m qwork backtest --symbol SPY --date 2026-10-02`: replay historical quotes.
- `.venv/Scripts/python.exe -m unittest discover -s tests -v`: run financial, GUI and native integration tests.
- `.venv/Scripts/python.exe -m ruff check src tests`: check imports and style; use `ruff format src tests` for formatting.

## Coding Style & Naming Conventions

Use four-space indentation, snake_case functions and PascalCase classes. Validate inputs with Pydantic. Keep calculation functions independent of networking and GUI code. Nautilus owns orders, fills, positions and cash accounting; do not create a parallel execution ledger. Construct, run and dispose native nodes on their owning thread.

## Testing Guidelines

Use unittest and `test_*.py` names. Test consequential financial changes with independent payoff, quote-direction and commission checks. Keep mathematical fixtures labelled; skipped integrations or synthetic data do not establish market performance. No numerical coverage target is configured.

## Commit & Pull Request Guidelines

No Git history is available. Use concise imperative subjects. Describe user-visible behavior, validation, model assumptions and unverified integrations. Include screenshots for GUI changes.

## Security & Configuration

Keep credentials in ignored `.env` and `data/private/`. Never log SDK authentication responses. Consult `docs/validation.md` before claiming live execution, tick-level validation or production readiness.
