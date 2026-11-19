import hashlib
import json

import pytest

from noroshi.registries import fetch_snapshots, load


@pytest.fixture
def fixture_registries(tmp_path):
    (tmp_path / "program-list.json").write_text(json.dumps([
        {"program_name": "HCA", "policy_url": "https://www.hcahealthcare.com/vdp", "securitytxt_url": "", "contact_email": ""},
        {"program_name": "Mail", "policy_url": "", "securitytxt_url": "", "contact_email": "sec@mailonly.org"},
        {"program_name": "Txt", "policy_url": "", "securitytxt_url": "https://txt.example.co.uk/.well-known/security.txt", "contact_email": ""},
    ]))
    (tmp_path / "CNAsList.json").write_text(json.dumps([{
        "shortName": "philips",
        "contact": [{"email": [{"emailAddr": "psirt@philips.com"}], "contact": [], "form": []}],
        "disclosurePolicy": [{"url": "https://www.philips.com/security"}],
        "securityAdvisories": {"alerts": [], "advisories": [{"url": "https://advisories.vendor.net/x"}]},
    }]))
    (tmp_path / "hackerone_data.json").write_text(json.dumps([{"targets": {"in_scope": [
        {"asset_identifier": "*.h1corp.com", "asset_type": "WILDCARD"},
        {"asset_identifier": "com.app.id", "asset_type": "GOOGLE_PLAY_APP_ID"},
        {"asset_identifier": "10.0.0.0/8", "asset_type": "CIDR"}]}}]))
    (tmp_path / "bugcrowd_data.json").write_text(json.dumps([{"targets": {"in_scope": [
        {"target": "https://api.bcorp.org/v1", "uri": "https://api.bcorp.org/v1"}]}}]))
    (tmp_path / "intigriti_data.json").write_text(json.dumps([{"targets": {"in_scope": [
        {"endpoint": "www.icorp.nl/path", "type": "url"}, {"endpoint": "all", "type": "other"}]}}]))
    (tmp_path / "yeswehack_data.json").write_text(json.dumps([{"targets": {"in_scope": [
        {"target": "https://yw.fr", "type": "web-application"}]}}]))
    return tmp_path


def test_registry_matching(fixture_registries):
    r = load(fixture_registries)
    assert r.hits("philips.com")["cna"] and r.hits("hcahealthcare.com")["diodb"]
    assert r.hits("nohits.org") == {"platforms": [], "diodb": False, "cna": False}


def test_registry_matching_fields(fixture_registries):
    r = load(fixture_registries)
    assert r.hits("mailonly.org")["diodb"] and r.hits("example.co.uk")["diodb"]
    assert r.hits("vendor.net")["cna"]
    assert r.hits("h1corp.com")["platforms"] == ["hackerone"]
    assert r.hits("bcorp.org")["platforms"] == ["bugcrowd"]
    assert r.hits("icorp.nl")["platforms"] == ["intigriti"]
    assert r.hits("yw.fr")["platforms"] == ["yeswehack"]
    assert r.hits("app.id")["platforms"] == []
    assert r.hits("www.philips.com")["cna"]  # a host is reduced to its registrable domain


def test_only_host_like_values_enter_the_platform_sets(tmp_path):
    """B14: a value is host-like when it parses to a host with a recognized public suffix; an @ separates an
    e-mail address only in a value with no ://."""
    (tmp_path / "hackerone_data.json").write_text(json.dumps([{"targets": {"in_scope": [
        {"asset_identifier": "https://www.pathcorp.com/@team/app", "asset_type": "URL"},
        {"asset_identifier": "https://user@userinfo.org/login", "asset_type": "URL"},
        {"asset_identifier": "internal.corp, build.local", "asset_type": "OTHER"},
        {"asset_identifier": "github.io", "asset_type": "URL"},
        {"asset_identifier": "sec@mailcorp.com", "asset_type": "OTHER"}]}}]))
    for p in ("bugcrowd", "intigriti", "yeswehack"):
        (tmp_path / f"{p}_data.json").write_text("[]")
    (tmp_path / "program-list.json").write_text(json.dumps([{"policy_url": "https://x.org/@sec", "contact_email": ""}]))
    (tmp_path / "CNAsList.json").write_text("[]")
    r = load(tmp_path)
    assert r.platforms["hackerone"] == {"pathcorp.com", "userinfo.org", "mailcorp.com"}
    assert r.diodb == {"x.org"}


def test_fetch_snapshots_pins_commit_and_hash(tmp_path):
    sha = "a" * 40

    class R:
        def __init__(self, content=b"", js=None):
            self.content, self._js = content, js

        def raise_for_status(self):
            pass

        def json(self):
            return self._js

    calls = []

    def get(url, **kw):
        calls.append(url)
        if url.startswith("https://api.github.com/"):
            return R(js={"sha": sha})
        assert sha in url  # downloads are pinned to the commit
        return R(content=json.dumps([{"x": 1}, {"x": 2}]).encode())

    fetch_snapshots(tmp_path / "ref", get=get, big_dir=tmp_path / "big")
    meta = json.loads((tmp_path / "ref" / "registries.meta.json").read_text())
    entry = meta["files"]["program-list.json"]
    assert entry["commit"] == sha and entry["entries"] == 2
    assert entry["sha256"] == hashlib.sha256((tmp_path / "ref" / "program-list.json").read_bytes()).hexdigest()
    assert len(meta["files"]) == 6 and (tmp_path / "ref" / "hackerone_data.json").exists()


def test_fetch_snapshots_big_file_goes_to_raw(tmp_path):
    class R:
        content = b"[1]"

        def raise_for_status(self):
            pass

        def json(self):
            return {"sha": "b" * 40}

    fetch_snapshots(tmp_path / "ref", get=lambda url, **kw: R(), big_dir=tmp_path / "big", max_bytes=2)
    assert (tmp_path / "big" / "hackerone_data.json").exists() and not (tmp_path / "ref" / "hackerone_data.json").exists()
    meta = json.loads((tmp_path / "ref" / "registries.meta.json").read_text())
    assert meta["files"]["hackerone_data.json"]["stored_in"] == "big"
