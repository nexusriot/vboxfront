Package: vboxfront
Version: @VERSION@
Section: utils
Priority: optional
Architecture: @ARCH@
Installed-Size: @SIZE@
Recommends: virtualbox
Maintainer: Vlad Ananyev <vlananyev@gmail.com>
Homepage: https://github.com/vlananyev/vboxfront
Description: PyQt6 frontend for VBoxManage disk operations
 VBoxFront is a small desktop frontend that wraps the disk-related
 VBoxManage subcommands (list, info, compact, resize, clone/convert,
 import-raw) in a single window. Every command is shown verbatim and
 its output is streamed live, so it stays transparent about what it
 runs.
 .
 VirtualBox must be installed separately to provide VBoxManage on PATH.
