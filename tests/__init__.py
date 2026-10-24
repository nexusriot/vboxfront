import os
import tempfile

# Offscreen Qt so the suite runs without a display, and a throwaway config
# home so QSettings writes (library, custom VBoxManage path) never touch the
# real user configuration. The data home goes with it: the delete trash falls
# back to it when an image's own folder cannot be written.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vboxfront-tests-")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="vboxfront-tests-data-")
os.environ["VBOX_USER_HOME"] = tempfile.mkdtemp(prefix="vboxfront-tests-vbox-")
