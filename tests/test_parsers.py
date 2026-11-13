import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml"))

from app import parsers  # noqa: E402
from app.config import settings, vmid_to_ip  # noqa: E402
from app.updater import ContainerInfo, compose_command, valid_pkg  # noqa: E402


def test_vmid_to_ip():
    assert vmid_to_ip(101) == "10.2.1.1"
    assert vmid_to_ip(130) == "10.2.1.30"
    assert vmid_to_ip(201) == "10.2.2.1"
    assert vmid_to_ip(9000) == "10.2.90.0"


def test_apt_upgradable_debian_and_ubuntu():
    out = """Listing...
apache2/oldstable-security 2.4.71-1~deb12u1 amd64 [upgradable from: 2.4.68-1~deb12u1]
openssl/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]
tzdata/stable-updates 2025b-0+deb12u1 all [upgradable from: 2025a-0+deb12u1]
"""
    r = {u.name: u for u in parsers.parse_apt_upgradable(out)}
    assert r["apache2"].security and r["apache2"].installed == "2.4.68-1~deb12u1"
    assert r["openssl"].security and r["openssl"].candidate == "3.0.13-0ubuntu3.5"
    assert not r["tzdata"].security


def test_dpkg_installed_ignores_removed():
    out = "apache2\t2.4.62\tii \nnginx\t1.22\trc \n"
    assert parsers.parse_dpkg_installed(out) == {"apache2": "2.4.62"}


def test_systemctl():
    assert parsers.parse_systemctl_is_active(["a", "b", "c"], "active\nfailed\n") == \
        {"a": "active", "b": "failed", "c": "unknown"}


def test_groups():
    assert settings.group_of("apache2-bin").name == "Apache"
    assert settings.group_of("php8.2-fpm").name == "PHP-FPM"
    assert settings.group_of("php8.2-mysql").name == "PHP"
    assert settings.group_of("libc6") is None


def test_valid_pkg():
    assert valid_pkg("apache2") and valid_pkg("libssl3t64") and valid_pkg("g++") and valid_pkg("libc6:amd64")
    assert not valid_pkg("foo;rm -rf /") and not valid_pkg("-o") and not valid_pkg("$(id)")


def test_compose_command_quotes():
    c = ContainerInfo(1, "x", "img", "proj", "web", "/opt/my app", "/opt/my app/compose.yaml")
    cmd = compose_command(c)
    assert "cd '/opt/my app'" in cmd and "-f '/opt/my app/compose.yaml'" in cmd and "pull web" in cmd
    assert compose_command(ContainerInfo(1, "x", "img", None, None, None, None)) is None
