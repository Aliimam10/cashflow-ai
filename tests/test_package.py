"""Package-level smoke tests."""


def test_package_imports() -> None:
    """The installed package exposes its release version."""
    import cashflow_ai

    assert cashflow_ai.__version__ == "1.0.0"
