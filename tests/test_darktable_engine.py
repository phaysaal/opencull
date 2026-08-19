import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from darktable_engine import (
    DARKTABLE_WORKFLOW,
    DarktableError,
    _demosaic_params,
    _xmp_with_demosaic,
    find_darktable_cli,
    render_darktable_default,
)


class DarktableEngineTests(unittest.TestCase):
    def test_isolated_default_render_records_engine_and_does_not_write_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = root / "darktable-cli"
            executable.write_text("test executable", encoding="utf-8")
            executable.chmod(0o755)
            source = root / "source" / "A.JPG"
            source.parent.mkdir()
            Image.new("RGB", (32, 24), (80, 100, 120)).save(source)

            commands = []

            def runner(command, **kwargs):
                commands.append(command)
                if command[-1] == "--version":
                    return subprocess.CompletedProcess(
                        command, 0, "darktable 5.6.0\n", "")
                shutil.copy2(command[1], command[2])
                return subprocess.CompletedProcess(command, 0, "", "")

            packet = (
                '<rdf:li darktable:operation="demosaic" darktable:modversion="6" '
                'darktable:params="0000000000000000000000000104000001000000cdcc4c3e"/>')
            with patch("darktable_engine._xmp_packet", return_value=packet):
                result = render_darktable_default(
                    source, root / "output", executable=executable, runner=runner)

            self.assertTrue(Path(result["output"]["path"]).is_file())
            self.assertEqual(result["engine"]["version"], "5.6.0")
            self.assertEqual(result["recipe_execution"]["mode"], "native-control")
            self.assertFalse(source.with_suffix(source.suffix + ".xmp").exists())
            # darktable's own workflow defaults are wanted -- that is what
            # puts a tone mapping on the raw at all -- and the config
            # directory is thrown away per render, so nothing of the
            # photographer's own is picked up with them.
            self.assertIn("--apply-custom-presets", commands[1])
            self.assertIn("true", commands[1])
            self.assertIn(
                f"plugins/darkroom/workflow={DARKTABLE_WORKFLOW}", commands[1])
            self.assertEqual(
                result["engine"]["demosaic"]["requested"],
                "markesteijn-1-pass")

    def test_demosaic_history_rewrite_uses_darktable_xtrans_method_ids(self):
        packet = (
            '<rdf:li darktable:operation="demosaic" darktable:modversion="6" '
            'darktable:params="0000000000000000000000000104000001000000cdcc4c3e"/>')
        rewritten = _xmp_with_demosaic(packet, 3074)
        _, method = _demosaic_params(rewritten)
        self.assertEqual(method, 3074)

    def test_missing_explicit_tool_is_not_silently_accepted(self):
        with self.assertRaises(DarktableError):
            find_darktable_cli("/definitely/unavailable/darktable-cli")


if __name__ == "__main__":
    unittest.main()
