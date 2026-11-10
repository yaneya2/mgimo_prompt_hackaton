import pytest


def pytest_addoption(parser):
    parser.addoption("--run-live-model", action="store_true", default=False,
                     help="Call the real configured model (uses account limits/credits)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-model"):
        return
    skip = pytest.mark.skip(reason="Real model disabled; pass --run-live-model explicitly")
    for item in items:
        if "live_model" in item.keywords:
            item.add_marker(skip)
