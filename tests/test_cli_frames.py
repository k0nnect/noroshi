"""A18: frame commands never overwrite an existing output without --force, and never once the protocol is frozen."""
import json

import pytest

from noroshi import cli


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("NOROSHI_ROOT", str(tmp_path))
    (tmp_path / "data" / "frames").mkdir(parents=True)
    (tmp_path / "data" / "reference").mkdir(parents=True)
    (tmp_path / "data" / "reference" / "rucc2023.csv").write_text("FIPS,State,County_Name,Attribute,Value\n")
    return tmp_path


@pytest.fixture
def fake_download(monkeypatch):
    from noroshi.frames import hospitals

    calls = []

    def download(out, url=None):
        calls.append(out)
        out.write_text(json.dumps({"features": [{"attributes": {
            "ID": "1", "NAME": "Andalusia Health", "STATE": "AL", "STATUS": "OPEN", "WEBSITE": "andalusia.org",
            "OWNER": "NON-PROFIT", "TYPE": "GENERAL ACUTE CARE", "BEDS": 25, "COUNTYFIPS": "01001"}}]}))
        return out

    monkeypatch.setattr(hospitals, "download_hifld", download)
    return calls


def test_existing_output_is_not_overwritten_without_force(root, fake_download):
    table = root / "data" / "frames" / "hospital_domains.csv"
    table.write_text("keep me\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["frames", "hospitals"])
    assert "--force" in str(e.value) and table.read_text() == "keep me\n" and fake_download == []
    cli.main(["frames", "hospitals", "--force"])
    assert fake_download and "andalusia.org" in table.read_text()


def test_new_outputs_need_no_force(root, fake_download):
    cli.main(["frames", "hospitals"])
    assert (root / "data" / "frames" / "hospital_domains.csv").exists()


def test_frozen_protocol_refuses_every_frame_command(root, fake_download, monkeypatch):
    from noroshi import resolve

    (root / "protocol").mkdir()
    (root / "protocol" / "FROZEN.txt").write_text("abc\n")
    monkeypatch.setattr(resolve, "memory_pass", lambda *a, **k: pytest.fail("no model call once frozen"))
    for argv in (["frames", "hospitals"], ["frames", "hospitals", "--force"], ["frames", "federal", "--force"],
                 ["frames", "companies", "--force"], ["frames", "audit", "--force"],
                 ["frames", "resolve", "device", "--force"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert "FROZEN" in str(e.value)
    assert fake_download == []


def test_resolve_refuses_to_overwrite_its_domain_table(root, monkeypatch):
    from noroshi import resolve

    (root / "data" / "frames" / "device_domains.csv").write_text("keep me\n")
    monkeypatch.setattr(resolve, "memory_pass", lambda *a, **k: pytest.fail("no model call when refusing"))
    with pytest.raises(SystemExit) as e:
        cli.main(["frames", "resolve", "device"])
    assert "--force" in str(e.value)
