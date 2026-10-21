"""Run the supplied engine tests unchanged, plus core-regression, adapter and API tests. No dependencies."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def original_suite():
    spec = importlib.util.spec_from_file_location("original_tests", ROOT / "tests/test_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.TestSuite()
    # The original tests leave WAL handles open during TemporaryDirectory.__exit__.
    # Windows cannot unlink them then. Defer cleanup until the test function returns.
    original_temp = tempfile.TemporaryDirectory
    class DeferredDirectory(original_temp):
        def __exit__(self, *args):
            pass
    for name in sorted(n for n in vars(module) if n.startswith("test_")):
        def run(fn=getattr(module, name)):
            directories = []
            class TrackedDirectory(DeferredDirectory):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    directories.append(self)
            tempfile.TemporaryDirectory = TrackedDirectory
            try:
                fn()
            finally:
                tempfile.TemporaryDirectory = original_temp
                for directory in directories:
                    directory.cleanup()
        suite.addTest(unittest.FunctionTestCase(run, description="Supplied engine: " + name))
    return suite


if __name__ == "__main__":
    suite = original_suite()
    for pattern in ("test_core.py", "test_agent.py", "test_product.py"):
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
