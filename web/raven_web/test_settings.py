import os
import tempfile

from .bootstrap import initialize

_test_state = tempfile.TemporaryDirectory(prefix="raven-web-tests-")
os.environ["RAVEN_WEB_STATE"] = _test_state.name
initialize(["--origin", "https://localhost:8443"])

from .settings import *
