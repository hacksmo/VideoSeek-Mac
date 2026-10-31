"""OS-level actions for a search result: drag it out, find it, open it.

Every result points at a file that already exists somewhere on disk, usually in
a folder the user registered. Once the hit list is useful, two questions follow
immediately: "how do I get this into another app" and "where does it actually
live". Both are delegated to the OS rather than reimplemented -- this is the
only module that shells out, and everything here is deliberately synchronous
because it is a direct response to a click.

The target workflow is dragging a photo straight into an editor (剪映/JianyingPro,
Final Cut, Premiere). Those apps accept a file URL drop, so the payload is
``text/uri-list`` built from the real source path -- never a re-encoded copy --
plus plain text for apps that only read strings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Sequence

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication


def existing_paths(paths: Iterable[object]) -> list[str]:
    """Keep the unique paths that resolve right now.

    A registered folder can be an external drive that is currently unplugged, and
    a row can survive a move until ``prune`` runs, so both cases are normal here
    rather than errors -- an empty list simply means "nothing to act on".
    """
    kept: list[str] = []
    seen: set[str] = set()
    for candidate in paths:
        path = str(candidate or "").strip()
        if not path or path in seen:
            continue
        if os.path.exists(path):
            seen.add(path)
            kept.append(path)
    return kept


def hit_path(hit: object, *keys: str) -> str:
    """First existing path among ``keys`` of a hit dict."""
    if not isinstance(hit, dict):
        return ""
    for key in keys:
        candidate = str(hit.get(key) or "").strip()
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def file_urls(paths: Iterable[object]) -> list[QUrl]:
    """``QUrl`` list for the paths that exist."""
    return [QUrl.fromLocalFile(path) for path in existing_paths(paths)]


def file_mime_data(paths: Iterable[object]) -> QMimeData:
    """Drag payload: file URLs plus their plain-text form.

    ``setUrls`` is what a Finder window, an editor's media bin or a terminal
    accepts. The text part covers the apps that only look at ``text/plain``.
    """
    mime = QMimeData()
    resolved = existing_paths(paths)
    if not resolved:
        return mime
    mime.setUrls([QUrl.fromLocalFile(path) for path in resolved])
    mime.setText("\n".join(resolved))
    return mime


def reveal_command(paths: Sequence[str], *, platform: str = "") -> list[str]:
    """Argument list that selects ``paths`` in the platform's file manager.

    Split out from :func:`reveal_in_file_manager` so each platform's shape can be
    asserted without launching anything.
    """
    resolved = [str(path or "").strip() for path in paths]
    resolved = [path for path in resolved if path]
    name = str(platform or sys.platform or "")
    if not resolved:
        return []
    if name.startswith("darwin"):
        # -R is "reveal": selects the items instead of opening them. Finder takes
        # several at once, so a multi-select reveals every selected file.
        return ["open", "-R", *resolved]
    if name.startswith("win"):
        # explorer takes the switch and the path as one token, and only one
        # selection per call, so extra selections are dropped there.
        return ["explorer", f"/select,{os.path.normpath(resolved[0])}"]
    # No portable "reveal" exists on Linux; opening the folder is the honest answer.
    return ["xdg-open", os.path.dirname(os.path.abspath(resolved[0])) or "."]


def reveal_in_file_manager(paths: Iterable[object]) -> bool:
    """Select the files in Finder / Explorer / the default file manager."""
    resolved = existing_paths(paths)
    command = reveal_command(resolved)
    if not command:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def open_with_default_app(path: str) -> bool:
    """Open the file the way a double-click would."""
    target = str(path or "").strip()
    if not target or not os.path.exists(target):
        return False
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(target)))


def copy_paths_to_clipboard(paths: Iterable[object]) -> bool:
    """Copy absolute paths as text, one per line."""
    resolved = existing_paths(paths)
    if not resolved:
        return False
    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return False
    clipboard.setText("\n".join(resolved))
    return True


def build_file_menu(parent, paths: Iterable[object], *, labels: dict):
    """Right-click menu for one or more files.

    Nothing to act on yields one inert item instead of a menu of shortcuts that
    do nothing: the usual cause is an external drive that is not mounted, and
    saying so beats a silent no-op.

    Returns ``(menu, {name: QAction})`` so a caller can preselect or disable an
    entry without rebuilding it.
    """
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu

    resolved = existing_paths(paths)
    menu = QMenu(parent)
    if not resolved:
        unavailable = QAction(str(labels.get("missing") or labels.get("menu_missing") or ""), menu)
        unavailable.setEnabled(False)
        menu.addAction(unavailable)
        return menu, {}

    actions = {}
    for key, handler in (
        ("reveal", lambda: reveal_in_file_manager(resolved)),
        ("copy_path", lambda: copy_paths_to_clipboard(resolved)),
        ("open", lambda: open_with_default_app(resolved[0])),
    ):
        label = str(labels.get(key) or labels.get(f"menu_{key}") or key)
        action = QAction(label, menu)
        action.triggered.connect(handler)
        menu.addAction(action)
        actions[key] = action
    return menu, actions


def enable_file_drag(view) -> None:
    """Turn a result view into a drag source.

    DragOnly: dropping onto ourselves has no meaning. Extended selection is what
    makes "select five photos, drag them into the editor" possible -- with single
    selection the gesture is one file at a time.

    The view's model supplies the URLs via ``mimeData``; callers still have to
    set a selection mode that matches what they want to drag.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView

    view.setDragEnabled(True)
    view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
    view.setDefaultDropAction(Qt.DropAction.CopyAction)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)


def attach_result_menu(view, model, *, field: str, labels) -> None:
    """Wire the right-click menu that reveals / copies / opens the source file.

    ``labels`` is a callable returning the current language table, because the
    menu is rebuilt on every click and must follow a language switch.

    A right-click on a row that is not part of the selection adopts just that
    row -- standard behaviour, and it keeps "right-click the one I meant" working
    when several rows are already selected.
    """

    def _popup(pos):
        rows = set()
        selection = view.selectionModel()
        if selection is not None:
            rows = {index.row() for index in selection.selectedIndexes()}
        index = view.indexAt(pos)
        if index.isValid() and index.row() not in rows:
            rows = {index.row()}
            if selection is not None:
                selection.select(
                    index,
                    selection.SelectionFlag.ClearAndSelect | selection.SelectionFlag.Current,
                )
        paths = [
            str((model.hit_at(row) or {}).get(field) or "") for row in sorted(rows)
        ]
        menu, _ = build_file_menu(view, paths, labels=labels())
        menu.exec(view.viewport().mapToGlobal(pos))

    from PySide6.QtCore import Qt

    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    view.customContextMenuRequested.connect(_popup)
