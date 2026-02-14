"""Tests for pi-gen build configuration."""

import pytest
from pathlib import Path
import stat


class TestPigenStructure:
    """Verify pi-gen directory structure is correct."""

    def test_pigen_directory_exists(self, pigen_dir: Path):
        assert pigen_dir.exists(), "deploy/pi-gen directory should exist"

    def test_config_file_exists(self, pigen_dir: Path):
        config = pigen_dir / "config"
        assert config.exists(), "config file should exist"

    def test_build_script_exists(self, pigen_dir: Path):
        build_sh = pigen_dir / "build.sh"
        assert build_sh.exists(), "build.sh should exist"

    def test_build_script_executable(self, pigen_dir: Path):
        build_sh = pigen_dir / "build.sh"
        assert build_sh.stat().st_mode & stat.S_IXUSR, "build.sh should be executable"

    def test_stage_directory_exists(self, pigen_dir: Path):
        stage_dir = pigen_dir / "stage-pi-decoder"
        assert stage_dir.exists(), "stage-pi-decoder should exist"

    def test_depends_file_exists(self, pigen_dir: Path):
        depends = pigen_dir / "stage-pi-decoder" / "DEPENDS"
        assert depends.exists(), "DEPENDS file should exist"


class TestPigenConfig:
    """Verify pi-gen config file contents."""

    def test_config_has_required_variables(self, pigen_dir: Path):
        config = pigen_dir / "config"
        content = config.read_text()

        required_vars = [
            "IMG_NAME",
            "RELEASE",
            "TARGET_HOSTNAME",
            "FIRST_USER_NAME",
            "STAGE_LIST",
        ]
        for var in required_vars:
            assert var in content, f"{var} should be in config"

    def test_config_stage_list_includes_custom_stage(self, pigen_dir: Path):
        config = pigen_dir / "config"
        content = config.read_text()
        assert "stage-pi-decoder" in content, "STAGE_LIST should include custom stage"

    def test_config_uses_bookworm(self, pigen_dir: Path):
        config = pigen_dir / "config"
        content = config.read_text()
        assert 'RELEASE="bookworm"' in content, "Should use bookworm release"


class TestPigenStage:
    """Verify custom stage structure."""

    def test_depends_on_stage3(self, pigen_dir: Path):
        depends = pigen_dir / "stage-pi-decoder" / "DEPENDS"
        content = depends.read_text().strip()
        assert content == "stage3", "Should depend on stage3"

    def test_packages_file_exists(self, pigen_dir: Path):
        packages = pigen_dir / "stage-pi-decoder" / "00-install-packages" / "00-packages"
        assert packages.exists(), "00-packages should exist"

    def test_packages_includes_required(self, pigen_dir: Path):
        packages = pigen_dir / "stage-pi-decoder" / "00-install-packages" / "00-packages"
        content = packages.read_text()

        required_packages = ["mpv", "python3-pip", "unclutter", "unattended-upgrades"]
        for pkg in required_packages:
            assert pkg in content, f"{pkg} should be in packages list"

    def test_install_app_script_exists(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "01-install-app" / "00-run.sh"
        assert script.exists(), "01-install-app/00-run.sh should exist"

    def test_install_app_script_executable(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "01-install-app" / "00-run.sh"
        assert script.stat().st_mode & stat.S_IXUSR, "Script should be executable"

    def test_configure_script_exists(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "02-configure" / "00-run.sh"
        assert script.exists(), "02-configure/00-run.sh should exist"

    def test_configure_script_executable(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "02-configure" / "00-run.sh"
        assert script.stat().st_mode & stat.S_IXUSR, "Script should be executable"

    def test_boot_config_script_exists(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "03-boot-config" / "00-run.sh"
        assert script.exists(), "03-boot-config/00-run.sh should exist"

    def test_boot_config_script_executable(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "03-boot-config" / "00-run.sh"
        assert script.stat().st_mode & stat.S_IXUSR, "Script should be executable"


class TestPigenConfigFiles:
    """Verify configuration files in 02-configure/files/."""

    @pytest.fixture
    def files_dir(self, pigen_dir: Path) -> Path:
        return pigen_dir / "stage-pi-decoder" / "02-configure" / "files"

    def test_unclutter_desktop_exists(self, files_dir: Path):
        assert (files_dir / "unclutter.desktop").exists()

    def test_disable_screensaver_desktop_exists(self, files_dir: Path):
        assert (files_dir / "disable-screensaver.desktop").exists()

    def test_panel_config_exists(self, files_dir: Path):
        assert (files_dir / "panel").exists()

    def test_desktop_items_config_exists(self, files_dir: Path):
        assert (files_dir / "desktop-items-0.conf").exists()

    def test_desktop_conf_exists(self, files_dir: Path):
        assert (files_dir / "desktop.conf").exists()

    def test_unattended_upgrades_exists(self, files_dir: Path):
        assert (files_dir / "50unattended-upgrades").exists()

    def test_auto_upgrades_exists(self, files_dir: Path):
        assert (files_dir / "20auto-upgrades").exists()

    def test_desktop_entry_format(self, files_dir: Path):
        """Verify desktop files have valid format."""
        for desktop_file in ["unclutter.desktop", "disable-screensaver.desktop"]:
            content = (files_dir / desktop_file).read_text()
            assert "[Desktop Entry]" in content
            assert "Type=Application" in content
            assert "Exec=" in content


class TestPigenScriptContent:
    """Verify script content uses correct pi-gen conventions."""

    def test_install_app_uses_rootfs_dir(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "01-install-app" / "00-run.sh"
        content = script.read_text()
        assert "${ROOTFS_DIR}" in content, "Should use ROOTFS_DIR variable"
        assert "${STAGE_DIR}" in content, "Should use STAGE_DIR variable"
        assert "on_chroot" in content, "Should use on_chroot for pip install"

    def test_configure_uses_first_user_name(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "02-configure" / "00-run.sh"
        content = script.read_text()
        assert "${FIRST_USER_NAME}" in content, "Should use FIRST_USER_NAME variable"
        assert "on_chroot" in content, "Should use on_chroot for systemctl"

    def test_boot_config_uses_correct_path(self, pigen_dir: Path):
        script = pigen_dir / "stage-pi-decoder" / "03-boot-config" / "00-run.sh"
        content = script.read_text()
        assert "/boot/firmware/config.txt" in content, "Should use bookworm boot path"
        assert "hdmi_force_hotplug" in content, "Should configure HDMI"

    def test_scripts_use_bash_e(self, pigen_dir: Path):
        """All scripts should use #!/bin/bash -e for error handling."""
        scripts = [
            pigen_dir / "stage-pi-decoder" / "01-install-app" / "00-run.sh",
            pigen_dir / "stage-pi-decoder" / "02-configure" / "00-run.sh",
            pigen_dir / "stage-pi-decoder" / "03-boot-config" / "00-run.sh",
        ]
        for script in scripts:
            content = script.read_text()
            assert content.startswith("#!/bin/bash -e"), f"{script.name} should start with #!/bin/bash -e"
