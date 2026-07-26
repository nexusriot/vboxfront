import os
import tempfile

# Offscreen Qt so the suite runs without a display, and a throwaway config
# home so QSettings writes (library, custom VBoxManage path) never touch the
# real user configuration.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vboxfront-tests-")
