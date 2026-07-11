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
