import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_harness.node import resolve_node


class NodeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "harness.local.toml"
        self.node = self.directory / "node.exe"
        self.node.touch()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PATH": ""}, clear=True).start()
        self.which = patch("evolving_harness.node.shutil.which", return_value=None).start()
        self.run = patch("evolving_harness.node.subprocess.run", return_value=
                         subprocess.CompletedProcess([], 0, "v22.22.2\n", "")).start()

    def test_local_config_works_without_path(self):
        self.config.write_text("[pi]\nnode = 'node.exe'\n", encoding="utf-8")
        self.assertEqual(resolve_node(self.config), str(self.node.resolve()))
        self.which.assert_not_called()

    def test_environment_overrides_config(self):
        self.config.write_text("[pi]\nnode = 'missing.exe'\n", encoding="utf-8")
        with patch.dict(os.environ, {"EH_NODE": str(self.node)}):
            self.assertEqual(resolve_node(self.config), str(self.node.resolve()))

    def test_path_used_without_explicit_configuration(self):
        self.which.return_value = str(self.node)
        self.assertEqual(resolve_node(self.config), str(self.node))

    def test_bad_explicit_path_does_not_silently_fall_back(self):
        self.which.return_value = str(self.node)
        with patch.dict(os.environ, {"EH_NODE": str(self.directory / "missing.exe")}):
            with self.assertRaisesRegex(ValueError, "does not exist"):
                resolve_node(self.config)
        self.which.assert_not_called()

    def test_missing_node_has_actionable_message(self):
        with self.assertRaisesRegex(ValueError, "EH_NODE"):
            resolve_node(self.config)

    def test_old_version_is_rejected(self):
        self.which.return_value = str(self.node)
        self.run.return_value.stdout = "v20.19.0\n"
        with self.assertRaisesRegex(ValueError, ">=22.19"):
            resolve_node(self.config)

    def test_invalid_version_and_execution_timeout(self):
        self.which.return_value = str(self.node)
        self.run.return_value.stdout = "not-node"
        with self.assertRaises(ValueError):
            resolve_node(self.config)
        self.run.side_effect = subprocess.TimeoutExpired("node", 5)
        with self.assertRaisesRegex(ValueError, "Cannot run"):
            resolve_node(self.config)
