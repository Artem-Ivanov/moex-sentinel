import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "deploy/remote/compose.sh"
PROD_WRAPPER = REPO / "deploy/remote/compose.production.sh"


class RemoteComposeWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / "release"
        self.remote = self.release / "deploy/remote"
        self.operations = self.release / "operations"
        self.anchor = self.operations / "admission_ops.py"
        self.bin = self.root / "bin"
        self.remote.mkdir(parents=True)
        self.operations.mkdir()
        self.bin.mkdir()
        for source, target in (
            (WRAPPER, self.remote / "compose.sh"),
            (PROD_WRAPPER, self.remote / "compose.production.sh"),
        ):
            shutil.copy2(source, target)
        for name in ("compose.yml",):
            (self.release / name).write_text("services: {}\n")
        for name in ("compose.remote.yml", "compose.production.yml"):
            (self.remote / name).write_text("services: {}\n")
        (self.remote / ".env").write_text("RELEASE_TAG=legacy-release\n")
        (self.remote / ".env.production").write_text("RELEASE_TAG=legacy-release\n")
        self.calls = self.root / "docker-calls"
        docker = self.bin / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$DOCKER_CALLS"\n'
            'if [ "$1" = image ] && [ "$2" = inspect ]; then echo sha256:expected; fi\n'
        )
        docker.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "DOCKER_CALLS": str(self.calls),
            "REMOTE_ENV_FILE": str(self.remote / ".env"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def install_marker(self, mode="candidate", contour="TEST"):
        self.anchor.write_text("# installed operations anchor\n")
        self.anchor.chmod(0o600)
        overlay_name = f"compose-{contour.lower()}-{mode}.yml"
        image_name = f"compose-{contour.lower()}-{mode}.images"
        overlay = self.operations / overlay_name
        overlay.write_text(
            "services:\n  backend:\n    image: moex-sentinel-backend:p3-build-backend\n"
            "    mem_limit: 268435456\n    memswap_limit: 402653184\n    cpus: '0.7'\n"
        )
        overlay.chmod(0o600)
        image_manifest = self.operations / image_name
        image_manifest.write_text("moex-sentinel-backend:p3-build-backend\tsha256:expected\n")
        image_manifest.chmod(0o600)
        checksum = hashlib.sha256(overlay.read_bytes()).hexdigest()
        images_checksum = hashlib.sha256(image_manifest.read_bytes()).hexdigest()
        marker = {
            "schema": 1,
            "mode": mode,
            "operations_sha256": hashlib.sha256(self.anchor.read_bytes()).hexdigest(),
            "files": {
                mode: {
                    contour: {
                        "file": overlay_name,
                        "sha256": checksum,
                        "images_file": image_name,
                        "images_sha256": images_checksum,
                    }
                }
            },
        }
        path = self.operations / ".compose-ops-installed"
        path.write_text(json.dumps(marker))
        path.chmod(0o600)
        return overlay

    def invoke(self, contour="TEST", args=(), requested_mode=None, wrapper=None):
        self.calls.unlink(missing_ok=True)
        env = {**self.env, "REMOTE_CONTOUR": contour}
        if requested_mode is not None:
            env["REMOTE_OPERATION_MODE"] = requested_mode
        return subprocess.run(
            ["/bin/sh", str(wrapper or self.remote / "compose.sh"), *args],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_installed_wrapper_pins_images_and_caps_before_user_files(self):
        overlay = self.install_marker()
        user_file = self.remote / "user.yml"
        user_file.write_text("services: {}\n")
        result = self.invoke(args=("-f", str(user_file), "up", "-d", "backend"))
        assert result.returncode == 0, result.stderr
        compose_call = next(line for line in self.calls.read_text().splitlines() if line.startswith("compose "))
        files = [
            compose_call.index(str(path))
            for path in (self.release / "compose.yml", self.remote / "compose.remote.yml", overlay, user_file)
        ]
        assert files == sorted(files)
        assert "image inspect --format {{.Id}} moex-sentinel-backend:p3-build-backend" in self.calls.read_text()

    def test_hash_change_fails_closed_before_compose_is_invoked(self):
        overlay = self.install_marker()
        overlay.write_text(overlay.read_text() + "# changed\n")
        result = self.invoke()
        assert result.returncode != 0
        assert "overlay hash changed" in result.stderr
        assert "compose " not in (self.calls.read_text() if self.calls.exists() else "")

    def test_overlay_symlink_fails_closed(self):
        overlay = self.install_marker()
        original = overlay.with_suffix(".original")
        overlay.rename(original)
        overlay.symlink_to(original)
        result = self.invoke()
        assert result.returncode != 0
        assert "permissions are invalid" in result.stderr
        assert "compose " not in (self.calls.read_text() if self.calls.exists() else "")

    def test_requested_nonactive_mode_fails_closed(self):
        self.install_marker(mode="candidate")
        result = self.invoke(requested_mode="fallback")
        assert result.returncode != 0
        assert "does not match" in result.stderr
        assert "compose " not in (self.calls.read_text() if self.calls.exists() else "")

    def test_operations_release_without_marker_fails_closed(self):
        self.anchor.write_text("# installed operations anchor\n")
        self.anchor.chmod(0o600)
        result = self.invoke()
        assert result.returncode != 0
        assert "marker is missing" in result.stderr
        assert "compose " not in (self.calls.read_text() if self.calls.exists() else "")

    def test_installed_operations_anchor_hash_is_checked(self):
        self.install_marker()
        self.anchor.write_text("# changed operations anchor\n")
        result = self.invoke()
        assert result.returncode != 0
        assert "anchor hash changed" in result.stderr
        assert "compose " not in (self.calls.read_text() if self.calls.exists() else "")

    def test_p3_release_tag_without_anchor_or_marker_fails_closed(self):
        p3_release = self.root / "vps-20261009-p3-0123456789ab"
        (p3_release / "deploy/remote").mkdir(parents=True)
        shutil.copy2(WRAPPER, p3_release / "deploy/remote/compose.sh")
        (p3_release / "compose.yml").write_text("services: {}\n")
        (p3_release / "deploy/remote/compose.remote.yml").write_text("services: {}\n")
        result = self.invoke(wrapper=p3_release / "deploy/remote/compose.sh")
        assert result.returncode != 0
        assert "anchor is missing" in result.stderr

    def test_production_order_places_durable_config_after_production_base(self):
        overlay = self.install_marker(contour="PROD")
        self.env["REMOTE_ENV_FILE"] = str(self.remote / ".env.production")
        result = self.invoke(contour="PROD", wrapper=self.remote / "compose.production.sh")
        assert result.returncode == 0, result.stderr
        compose_call = next(line for line in self.calls.read_text().splitlines() if line.startswith("compose "))
        assert compose_call.index(str(self.remote / "compose.production.yml")) < compose_call.index(str(overlay))

    def test_fallback_marker_keeps_durable_fallback_overlay_active(self):
        overlay = self.install_marker(mode="fallback")
        result = self.invoke(contour="TEST", requested_mode="fallback")
        assert result.returncode == 0, result.stderr
        compose_call = next(line for line in self.calls.read_text().splitlines() if line.startswith("compose "))
        assert str(overlay) in compose_call

    def test_legacy_release_without_install_marker_keeps_original_wrapper_behavior(self):
        result = self.invoke(args=("ps",))
        assert result.returncode == 0, result.stderr
        compose_call = self.calls.read_text()
        assert str(self.release / "compose.yml") in compose_call
        assert "operations/compose-" not in compose_call


if __name__ == "__main__":
    unittest.main()
