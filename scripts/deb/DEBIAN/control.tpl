Package: vboxfront
Version: @VERSION@
Section: utils
Priority: optional
Architecture: @ARCH@
Installed-Size: @SIZE@
Recommends: virtualbox
Maintainer: Vlad Ananyev <vlananyev@gmail.com>
Homepage: https://github.com/nexusriot/vboxfront
Description: PyQt6 frontend for VBoxManage media operations
 VBoxFront is a small desktop frontend that wraps the media-related
 VBoxManage subcommands in a single window: list hard disks, DVD and
 floppy images (with differencing chains as a tree), create, compact,
 resize, clone/convert, import RAW, edit properties, move, encrypt,
 and attach/detach media to VMs. Every command is shown verbatim and
 its output is streamed live, so it stays transparent about what it
 runs.
 .
 VirtualBox must be installed separately to provide VBoxManage.
