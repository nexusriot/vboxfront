#!/usr/bin/env python3
"""CLI entry point and backwards-compatible imports for VBoxFront.

Implementation lives in vboxfront_app. New code imports its owning module.
"""

from vboxfront_app.version import APP_VERSION
from vboxfront_app.constants import (
    DISK_FORMATS,
    DISK_EXT,
    DISK_VARIANTS,
    VMDK_VARIANTS,
    MEDIUM_TYPES,
    ENCRYPTION_CIPHERS,
    PASSWORD_STDIN,
    HEX_DUMP_WARN_BYTES,
    CAP_CREATE_FIXED,
    CAP_CREATE_DYNAMIC,
    CAP_CREATE_SPLIT2G,
    CAP_DIFFERENCING,
    CAP_FILE,
    KINDS,
    KIND_META,
    MEDIUM_KEYS,
    MEDIUM_KEY_ORDER,
    COLUMNS,
    COL_NAME,
    COL_FORMAT,
    COL_CAPACITY,
    COL_SIZE,
    COL_TYPE,
    COL_STATE,
    COL_ENCRYPTION,
    COL_IN_USE,
    COL_SNAPSHOT,
    COL_LOCATION,
    COL_UUID,
    MEDIA_EXTENSIONS,
    RAW_EXTENSIONS,
    TWO_DEVICE_CONTROLLERS,
    TRASH_DIR_NAME,
    COMPRESSORS,
    STDIN_CHUNK,
    LONG_OPERATION_SECONDS,
    DESTRUCTIVE_FRAGMENTS,
    DESTRUCTIVE_PREFIXES,
    MIXED_TYPE,
)
from vboxfront_app.models import (
    MediumRecord,
    MediumBackend,
    TrashedFile,
    HealthFinding,
)
from vboxfront_app.domain import (
    parse_hddbackends,
    disk_backends,
    parse_systemproperties,
    parse_media_list,
    parse_capacity_mb,
    format_mbytes,
    parse_in_use_entry,
    parse_in_use_snapshot,
    snapshot_only,
    snapshot_word,
    parse_host_drives,
    controller_types,
    max_device_index,
    parse_vms_list,
    parse_machinereadable,
    vm_storage_controllers,
    find_attachment,
    find_free_port,
    free_slots,
    slot_occupant,
    parse_byte_size,
    is_differencing,
    real_parent_uuid,
    child_names,
    is_uuid,
    same_file,
    registered_at,
    medium_type_word,
    state_word,
    is_inaccessible,
    encryption_word,
    registry_fingerprint,
    totals_line,
    format_bytes,
    free_bytes_for,
    allocation_estimate,
    space_warning,
    EXPORT_FIELDS,
    medium_details,
    export_rows,
    records_to_csv,
    records_to_json,
    records_to_tsv,
    parse_uuid_collision,
    FILTER_FIELDS,
    FILTER_SIZES,
    FILTER_TERM,
    parse_filter,
    record_matches,
    scan_for_images,
    health_findings,
    discard_findings,
    health_report,
    is_destructive_command,
    rerun_blocker,
    format_duration,
    eta_text,
    reclaim_line,
)
from vboxfront_app.persistence import (
    get_settings,
    vboxmanage_path,
    vboxmanage_problem,
    _as_str_list,
    load_library,
    save_library,
    add_library_path,
    remove_library_path,
    plan_mode,
    set_plan_mode,
    trash_enabled,
    trash_root,
    user_trash_dir,
    trash_dir_for,
    unique_in,
    move_to_trash,
    restore_trashed,
    image_folder,
    remembered_folder,
    remember_folder,
    default_image_path,
    snapshot_dir,
    write_registry_snapshot,
    load_history,
    append_history,
    clear_history,
)
from vboxfront_app.theme import (
    _palette,
    dark_theme,
    muted_color,
    warning_color,
    error_color,
    color_css,
)
from vboxfront_app.commands import (
    plan_script,
    list_media_args,
    create_disk_args,
    attach_args,
    clone_args,
    mediumproperty_args,
    detach_args,
    properties_args,
    resize_args,
    move_args,
    encrypt_args,
    decrypt_args,
    check_password_args,
    mediumio_args,
    mediumio_cat_args,
    mediumio_formatfat_args,
    convertfromraw_stdin_args,
    export_raw_args,
    create_diff_args,
    create_floppy_args,
    autoreset_args,
    setlocation_args,
    repairhd_args,
    sethduuid_args,
    sethdparentuuid_args,
    change_password_args,
    write_secret_file,
    rerun_args,
)
from vboxfront_app.compression import (
    compressor_for,
    open_maybe_compressed,
    uncompressed_size,
    measure_uncompressed,
)
from vboxfront_app.widgets import (
    ElidingLabel,
    MediumItem,
    MediaPane,
    confirm_space,
)
from vboxfront_app.processes import CommandRunner, CaptureRunner
from vboxfront_app.disk_dialogs import (
    CreateDiskDialog,
    ResizeDialog,
    ConvertDialog,
    CreateDiffDialog,
    CreateFloppyDialog,
    ImportCompressedDialog,
    BatchConvertDialog,
)
from vboxfront_app.media_dialogs import (
    RemoveDialog,
    ContentsDialog,
    ResolveDialog,
    UuidToolsDialog,
    PropertiesDialog,
    EncryptDialog,
    MediumPropertyDialog,
)
from vboxfront_app.app_dialogs import (
    CommandHistoryDialog,
    SettingsDialog,
    PlanDialog,
    HealthDialog,
    OrphanScanDialog,
)
from vboxfront_app.attach_dialog import AttachDialog
from vboxfront_app.window import MainWindow, ask_target_format_and_path
from vboxfront_app.app import main

from PyQt6.QtCore import (
    QByteArray,
    QDateTime,
    QProcess,
    QSettings,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QKeySequence,
    QPalette,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from dataclasses import dataclass, field
import bz2
import csv
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shlex
import shutil
import sys
import tempfile

if __name__ == "__main__":
    main()
