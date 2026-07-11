# Task 3 Report: Observation-Only Risk Evaluation

## RED evidence

Command:

`\.venv\Scripts\python.exe -m unittest tests.test_pre_trade_check tests.test_config_env -v`

Before implementation, the run failed with three expected errors: `ModuleNotFoundError` for
`pre_trade_check`, and missing `config.RISK_MODE` in both new configuration tests.

## GREEN evidence

Focused command:

`\.venv\Scripts\python.exe -m unittest tests.test_pre_trade_check tests.test_config_env -v`

Result: 5 tests passed.

Regression command:

`\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Result: 117 tests passed.

## Self-review and concerns

- Soft-limit warnings are deterministic and never block otherwise valid signals.
- Total-position and sector warnings use projected exposure for buys, as required by the supplied test.
- Invalid order input is the only hard block introduced here.
- `RISK_MODE` is normalized to lowercase and any mode other than `observe` raises during configuration load.
- No strategy, order, selection, scoring, or JoinQuant behavior was changed.
- The metrics mapping is intentionally a plain mapping inside a frozen result dataclass; callers should treat it as read-only.

## Review fixes

Fix commit: `402a0cbc782ef277b0eed47c582324aa47f5a260`

### RED evidence

Command: `\.venv\Scripts\python.exe -m unittest tests.test_pre_trade_check tests.test_config_env -v`

Result before the fix: 9 tests ran with exactly 3 failures:

- `test_valid_sell_without_position_pct_is_allowed`: valid sell was blocked.
- `test_portfolio_sector_exposure_is_defensively_immutable`: source mutation changed the instance.
- `test_result_metrics_are_defensively_immutable`: source mutation changed the result.

The new isolated-default and unsupported-mode configuration tests passed in this RED run.

### GREEN evidence

Focused command: `\.venv\Scripts\python.exe -m unittest tests.test_pre_trade_check tests.test_config_env -v`

Result: 9 tests passed.

Full command: `\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Result: 121 tests passed.

Review fixes make sell validation action-aware, defensively copy and wrap both mappings with
`MappingProxyType`, isolate default configuration tests from the process environment, and cover
unsupported risk modes. The prior concern about metrics being caller-mutable is resolved.
