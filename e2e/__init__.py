"""Hermetic end-to-end suite: the real app against a stateful fake VBoxManage.

Qt runs offscreen and QSettings is pointed at a throwaway config home, so the
suite needs no display and never touches the user's own configuration — the
same contract the unit suite keeps, and what lets `make e2e` run the two
against each other in one container. The data home is redirected for the same
reason: the delete trash falls back to it when an image's own folder cannot be
written.
"""

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="vboxfront-e2e-cfg-"))
os.environ.setdefault("XDG_DATA_HOME", tempfile.mkdtemp(prefix="vboxfront-e2e-data-"))
