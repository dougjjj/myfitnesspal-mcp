from myfitnesspal_mcp import cli, config


def test_ensure_private_dir_creates_0700(tmp_path):
    target = tmp_path / "myfitnesspal-mcp"
    config.ensure_private_dir(target)
    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700


def test_ensure_private_dir_does_not_chmod_foreign_existing(tmp_path):
    foreign = tmp_path / "shared-parent"
    foreign.mkdir(mode=0o755)
    config.ensure_private_dir(foreign)
    assert foreign.stat().st_mode & 0o777 == 0o755


def test_read_only_env(monkeypatch):
    monkeypatch.delenv("MFP_READ_ONLY", raising=False)
    assert config.read_only() is False
    monkeypatch.setenv("MFP_READ_ONLY", "YES")
    assert config.read_only() is True
    monkeypatch.setenv("MFP_READ_ONLY", "0")
    assert config.read_only() is False


def test_http_bind_loopback():
    assert cli.http_bind_is_loopback("127.0.0.1")
    assert cli.http_bind_is_loopback("localhost")
    assert cli.http_bind_is_loopback("::1")
    assert not cli.http_bind_is_loopback("0.0.0.0")
    assert not cli.http_bind_is_loopback("192.168.1.10")