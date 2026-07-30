"""Package-level smoke tests."""


def test_package_imports() -> None:
    """The installed package exposes its initial version."""
    import cashflow_ai

    assert cashflow_ai.__version__ == "0.1.0"
