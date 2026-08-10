"""Explicit versioned registry for active strategy factor paths."""

if "FACTOR_PATH_MOMENTUM" not in globals():
    from factor_contracts import (
        FACTOR_PATH_LIMITDOWN,
        FACTOR_PATH_MOMENTUM,
        FACTOR_PATH_WAVE3,
    )
if "evaluate_wave3_factor" not in globals():
    from factor_wave3 import WAVE3_FACTOR_VERSION, evaluate_wave3_factor
if "evaluate_limitdown_exhaustion_factor" not in globals():
    from factor_limitdown import (
        LIMITDOWN_FACTOR_VERSION,
        evaluate_limitdown_exhaustion_factor,
    )


FACTOR_REGISTRY_VERSION = "2026-08-10.1"


def active_factor_registry():
    return {
        FACTOR_PATH_MOMENTUM: {
            "version": "existing-live-v1",
            "evaluator": None,
            "simulation_only": False,
            "position_cap_pct": None,
            "risk_budget_pct": None,
            "max_concurrent": None,
            "max_new_per_day": None,
        },
        FACTOR_PATH_WAVE3: {
            "version": WAVE3_FACTOR_VERSION,
            "evaluator": evaluate_wave3_factor,
            "simulation_only": True,
            "position_cap_pct": 12.0,
            "risk_budget_pct": 0.50,
            "max_concurrent": 2,
            "max_new_per_day": 1,
        },
        FACTOR_PATH_LIMITDOWN: {
            "version": LIMITDOWN_FACTOR_VERSION,
            "evaluator": evaluate_limitdown_exhaustion_factor,
            "simulation_only": True,
            "position_cap_pct": 8.0,
            "risk_budget_pct": 0.35,
            "max_concurrent": 1,
            "max_new_per_day": 1,
        },
    }


def factor_registry_manifest():
    manifest = {"registry_version": FACTOR_REGISTRY_VERSION, "paths": {}}
    for path, item in active_factor_registry().items():
        manifest["paths"][path] = {
            key: value for key, value in item.items() if key != "evaluator"
        }
    return manifest
