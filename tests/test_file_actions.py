"""Result -> file actions: drag payloads, reveal, copy, and the right-click menu.

Search results point at real files, and the two things a user does next are
"get this into another app" (drag) and "where does it live" (reveal). Nothing
here touches retrieval, so these are small pure-Python plus offscreen-Qt tests.

The platform branch is worth pinning precisely: ``open -R`` takes several paths,
Windows' ``explorer /select,`` takes one, and Linux has no reveal at all. A wrong
shape fails silently at runtime rather than raising.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _make_files():
    """Two real files with awkward names -- spaces, CJK, apostrophes."""
    tmp = tempfile.TemporaryDirectory()
    paths = []
    for name in ("plain.jpg", "有 空格 的图.jpg", "it's it.png"):
        path = os.path.join(tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n")
        paths.append(path)
    return tmp, paths


class PathResolutionTests(unittest.TestCase):
    def test_only_existing_paths_survive_and_duplicates_collapse(self):
        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)
        missing = os.path.join(tmp.name, "gone.jpg")

        resolved = file_actions.existing_paths([paths[0], missing, paths[0], "", None, " "])

        self.assertEqual(resolved, [paths[0]])

    def test_hit_path_prefers_the_first_key_that_resolves(self):
        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)

        self.assertEqual(
            file_actions.hit_path(
                {"mirage": "/nope.jpg", "image_path": paths[1]},
                "mirage",
                "image_path",
            ),
            paths[1],
        )

    def test_a_missing_hit_resolves_to_nothing(self):
        from ui import file_actions

        self.assertEqual(file_actions.hit_path({"image_path": "/nope.jpg"}, "image_path"), "")
        self.assertEqual(file_actions.hit_path(None, "image_path"), "")


class DragPayloadTests(unittest.TestCase):
    def test_urls_are_local_files_and_survive_odd_names(self):
        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)

        urls = file_actions.file_urls(paths)

        self.assertEqual(len(urls), 3)
        self.assertEqual(urls[0].scheme(), "file")
        with patch.object(file_actions.os.path, "exists", return_value=False):
            self.assertEqual(file_actions.file_urls(paths), [])

    def test_mime_data_carries_urls_and_text(self):
        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)

        mime = file_actions.file_mime_data(paths[:2])

        self.assertTrue(mime.hasUrls())
        self.assertEqual([url.toLocalFile() for url in mime.urls()], paths[:2])
        # Apps that only read text/plain must still see the absolute paths.
        self.assertEqual(mime.text().splitlines(), paths[:2])

    def test_nothing_to_drag_yields_an_empty_payload(self):
        from ui import file_actions

        mime = file_actions.file_mime_data(["/definitely/not/here.jpg"])

        self.assertFalse(mime.hasUrls())
        self.assertEqual(mime.text(), "")


class RevealCommandTests(unittest.TestCase):
    def test_macos_reveals_every_selected_file(self):
        from ui import file_actions

        command = file_actions.reveal_command(["/a/b.jpg", "/c d/e.jpg"], platform="darwin")

        self.assertEqual(command[0:2], ["open", "-R"])
        self.assertEqual(command[2:], ["/a/b.jpg", "/c d/e.jpg"])

    def test_windows_selects_a_single_file(self):
        from ui import file_actions

        command = file_actions.reveal_command(["/a/b.jpg", "/c/e.jpg"], platform="win32")

        self.assertEqual(len(command), 2)
        self.assertEqual(command[0], "explorer")
        self.assertTrue(command[1].startswith("/select,"))

    def test_linux_opens_the_folder_because_there_is_no_portable_reveal(self):
        from ui import file_actions

        command = file_actions.reveal_command(["/a/b.jpg"], platform="linux")

        self.assertEqual(command, ["xdg-open", "/a"])

    def test_empty_input_means_no_command(self):
        from ui import file_actions

        self.assertEqual(file_actions.reveal_command([], platform="darwin"), [])

    def test_reveal_builds_the_platform_command_and_skips_missing_files(self):
        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)

        launched = []
        with patch.object(
            file_actions.subprocess, "Popen", side_effect=lambda argv, **k: launched.append(argv)
        ):
            ok = file_actions.reveal_in_file_manager([paths[0], "/nope.jpg"])
            never = file_actions.reveal_in_file_manager(["/nope.jpg"])

        self.assertTrue(ok)
        self.assertFalse(never)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0], file_actions.reveal_command([paths[0]]))
        self.assertIn(paths[0], launched[0])

    def test_a_failed_launch_reports_false_instead_of_raising(self):
        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)

        with patch.object(file_actions.subprocess, "Popen", side_effect=OSError("boom")):
            self.assertFalse(file_actions.reveal_in_file_manager([paths[0]]))


class ClipboardAndOpenTests(unittest.TestCase):
    def test_copy_puts_one_absolute_path_per_line(self):
        from ui import file_actions

        _qt_app()
        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)

        self.assertTrue(file_actions.copy_paths_to_clipboard(paths[:2]))

        from PySide6.QtWidgets import QApplication

        self.assertEqual(
            QApplication.clipboard().text().splitlines(),
            paths[:2],
        )

    def test_copy_is_false_when_there_is_nothing_to_copy(self):
        from ui import file_actions

        _qt_app()

        self.assertFalse(file_actions.copy_paths_to_clipboard(["/nope.jpg"]))

    def test_open_refuses_a_missing_file(self):
        from ui import file_actions

        _qt_app()

        self.assertFalse(file_actions.open_with_default_app("/nope.jpg"))


class ResultMenuTests(unittest.TestCase):
    def setUp(self):
        _qt_app()
        from ui import file_actions

        self.file_actions = file_actions
        tmp, self.paths = _make_files()
        self.addCleanup(tmp.cleanup)
        self.labels = {
            "menu_reveal": "在访达中显示",
            "menu_copy_path": "复制文件路径",
            "menu_open": "用默认应用打开",
            "menu_missing": "原文件不可达",
        }

    def test_menu_offers_the_three_actions_with_the_local_labels(self):
        menu, actions = self.file_actions.build_file_menu(
            None, [self.paths[0]], labels=self.labels
        )

        self.assertEqual(
            [action.text() for action in menu.actions()],
            ["在访达中显示", "复制文件路径", "用默认应用打开"],
        )
        self.assertEqual(set(actions), {"reveal", "copy_path", "open"})

    def test_an_unreachable_file_gets_one_inert_item(self):
        menu, actions = self.file_actions.build_file_menu(
            None, ["/Volumes/Gone/a.jpg"], labels=self.labels
        )

        self.assertEqual(len(menu.actions()), 1)
        self.assertFalse(menu.actions()[0].isEnabled())
        self.assertEqual(menu.actions()[0].text(), "原文件不可达")
        self.assertEqual(actions, {})


class ViewWiringTests(unittest.TestCase):
    def setUp(self):
        _qt_app()
        from PySide6.QtWidgets import QListView

        from ui.widgets.image_library_page import ImageGridModel

        self.model = ImageGridModel()
        self.view = QListView()
        self.view.setModel(self.model)

    def _fill(self, paths):
        self.model.set_hits(
            [{"image_path": path} for path in paths],
            [f"key{index}" for index in range(len(paths))],
        )

    def test_drag_enabled_turns_the_view_into_a_copy_only_source(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QAbstractItemView

        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)
        self._fill(paths)

        file_actions.enable_file_drag(self.view)

        self.assertTrue(self.view.dragEnabled())
        self.assertEqual(
            self.view.dragDropMode(), QAbstractItemView.DragDropMode.DragOnly
        )
        self.assertEqual(self.view.defaultDropAction(), Qt.DropAction.CopyAction)
        # Extended selection is what makes "select five, drag five" possible.
        self.assertEqual(
            self.view.selectionMode(), QAbstractItemView.SelectionMode.ExtendedSelection
        )

    def test_model_hands_the_source_paths_to_the_drop_target(self):
        from PySide6.QtCore import Qt

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)
        self._fill(paths)

        indexes = [self.model.index(row, 0) for row in range(len(paths))]
        mime = self.model.mimeData(indexes)

        self.assertEqual([url.toLocalFile() for url in mime.urls()], paths)
        self.assertEqual(self.model.supportedDragActions(), Qt.DropAction.CopyAction)

    def test_missing_rows_are_not_offered_as_urls(self):
        self._fill(["/Volumes/Gone/a.jpg"])

        mime = self.model.mimeData([self.model.index(0, 0)])

        self.assertEqual(mime.urls(), [])

    def test_right_click_on_an_unselected_row_adopts_that_row(self):
        from PySide6.QtCore import QPoint

        from ui import file_actions

        tmp, paths = _make_files()
        self.addCleanup(tmp.cleanup)
        self._fill(paths)
        labels = {"menu_reveal": "R", "menu_copy_path": "C", "menu_open": "O", "menu_missing": "M"}

        captured = {}

        class _FakeMenu:
            def __init__(self):
                self.executed = False

            def exec(self, pos=None):  # noqa: A003 - Qt's exec is the API under test
                self.executed = True
                return None

        def fake_popup(parent, paths, *, labels):
            captured["paths"] = list(paths)
            captured["labels"] = labels
            return _FakeMenu(), {}

        file_actions.attach_result_menu(
            self.view,
            self.model,
            field="image_path",
            labels=lambda: labels,
        )
        with patch.object(file_actions, "build_file_menu", side_effect=fake_popup):
            # Select row 0 first: a click elsewhere must override the selection
            # rather than acting on something the user did not click.
            self.view.selectionModel().select(
                self.model.index(0, 0),
                self.view.selectionModel().SelectionFlag.Select,
            )
            self.view.customContextMenuRequested.emit(QPoint(2, 2))

        self.assertEqual(captured["paths"], [paths[0]])
        self.assertEqual(captured["labels"]["menu_reveal"], "R")


if __name__ == "__main__":
    unittest.main()
