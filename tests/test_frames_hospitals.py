import json

import pandas as pd
import pytest

from noroshi.frames import name_tokens
from noroshi.frames.hospitals import audit_candidates, build_hospital_frame, download_hifld, load_rucc


def rec(id_, name, state="AL", status="OPEN", website="NOT AVAILABLE", owner="NON-PROFIT",
        type_="GENERAL ACUTE CARE", beds=50, fips="01001", trauma="NOT AVAILABLE"):
    return {"attributes": {"ID": id_, "NAME": name, "STATE": state, "STATUS": status,
                           "WEBSITE": website, "OWNER": owner, "TYPE": type_, "BEDS": beds,
                           "COUNTYFIPS": fips, "TRAUMA": trauma}}


FIXTURE = [
    rec("closed1", "Old Closed Hospital", status="CLOSED", website="https://www.closed.org"),
    rec("va1", "VA Medical Center", owner="GOVERNMENT - FEDERAL", website="https://www.va.gov"),
    rec("mil1", "Army Hospital", type_="MILITARY", website="https://army.example.mil"),
    rec("pr1", "Puerto Rico Hospital", state="PR", website="https://www.prh.org"),
    rec("nosite1", "No Site Hospital"),
    rec("bad1", "Bad Site Hospital", website="not a host"),
    rec("fb1", "Facebook Hospital", website="https://www.facebook.com/somehospital"),
    rec("big1", "Big One Hospital", website="https://www.bigsystem.org/one", beds=88, fips="01003"),
    rec("big2", "Big Two Hospital", website="https://two.bigsystem.org", beds=71, fips="01005"),
    rec("big3", "Big Three Hospital", website="https://www.bigsystem.org/three", beds=51, fips="01005"),
    rec("neg1", "Negative Beds Hospital", website="http://negbeds.com", beds=-999, fips="01007"),
    rec("solo1", "Andalusia Health", website="andalusia.org", beds=25),
]


@pytest.fixture
def frame(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"features": FIXTURE}))
    rucc = tmp_path / "rucc.csv"
    rucc.write_text(
        "FIPS,State,County_Name,Attribute,Value\n"
        "01001,AL,A,RUCC_2023,2\n01001,AL,A,Description,metro\n"
        "01003,AL,B,RUCC_2023,3\n01005,AL,C,RUCC_2023,4\n01007,AL,D,RUCC_2023,9\n"
    )
    return build_hospital_frame(raw, rucc)


def test_exclusions_applied_in_order(frame):
    h, _ = frame
    ex = h.set_index("hifld_id")["exclusion"]
    assert ex["closed1"] == "closed"      # closed beats federal-looking rows and has a website
    assert ex["va1"] == "federal"
    assert ex["mil1"] == "federal"
    assert ex["pr1"] == "territory"
    assert ex["nosite1"] == "no_website"
    assert ex["bad1"] == "malformed"
    assert ex["solo1"] is None or pd.isna(ex["solo1"])


def test_order_closed_before_territory_before_federal():
    raw = [rec("x", "X", state="PR", status="CLOSED", owner="GOVERNMENT - FEDERAL")]
    # closed wins over territory and federal
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "r.json").write_text(json.dumps({"features": raw}))
    (d / "u.csv").write_text("FIPS,State,County_Name,Attribute,Value\n")
    h, _ = build_hospital_frame(d / "r.json", d / "u.csv")
    assert h.loc[0, "exclusion"] == "closed"
    raw = [rec("y", "Y", state="GU", owner="GOVERNMENT - FEDERAL")]
    (d / "r.json").write_text(json.dumps({"features": raw}))
    h, _ = build_hospital_frame(d / "r.json", d / "u.csv")
    assert h.loc[0, "exclusion"] == "territory"


def test_shared_platform_excluded(frame):
    h, d = frame
    assert h.set_index("hifld_id").loc["fb1", "exclusion"] == "shared_platform"
    assert "facebook.com" not in set(d.domain)


def test_excluded_rows_have_no_host_or_domain(frame):
    h, d = frame
    assert h[h.exclusion.notna()][["host", "domain"]].isna().all().all()
    assert set(d.domain) == {"bigsystem.org", "negbeds.com", "andalusia.org"}


def test_system_hospitals_share_one_domain(frame):
    _, d = frame
    row = d.set_index("domain").loc["bigsystem.org"]
    assert row.n_units == 3 and row.beds_sum == 88 + 71 + 51
    assert row.sector == "hospital"


def test_domain_table_columns(frame):
    _, d = frame
    assert list(d.columns) == ["sector", "domain", "start_url", "n_units", "beds_sum", "name_tokens"]


def test_negative_beds_become_missing(frame):
    h, d = frame
    assert pd.isna(h.set_index("hifld_id").loc["neg1", "beds"])
    assert pd.isna(d.set_index("domain").loc["negbeds.com", "beds_sum"])  # all missing: unknown, not zero


def test_name_tokens():
    assert name_tokens(["ANDALUSIA HEALTH", "Athens Limestone Hospital"]) == "andalusia athens limestone"


def test_name_tokens_drops_short_words_and_stopwords():
    assert name_tokens(["St Mary Medical Center Inc", "Memorial Healthcare System"]) == "mary"


def test_start_url_uses_most_common_host(frame):
    _, d = frame
    # www.bigsystem.org twice, two.bigsystem.org once
    assert d.set_index("domain").loc["bigsystem.org", "start_url"] == "https://www.bigsystem.org/"


def test_start_url_tie_broken_by_shortest_host(tmp_path):
    raw = tmp_path / "r.json"
    raw.write_text(json.dumps({"features": [
        rec("a", "A", website="https://longer.tie.org"), rec("b", "B", website="https://tie.org")]}))
    (tmp_path / "u.csv").write_text("FIPS,State,County_Name,Attribute,Value\n")
    _, d = build_hospital_frame(raw, tmp_path / "u.csv")
    assert d.loc[0, "start_url"] == "https://tie.org/"


def test_rural_flag_from_rucc(frame):
    h, _ = frame
    r = h.set_index("hifld_id")["rural"]
    assert r["closed1"] == False and r["big1"] == False   # RUCC 2 and 3
    assert r["big2"] == True                               # RUCC 4
    assert r["neg1"] == True                               # RUCC 9


def test_load_rucc_long_format(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("FIPS,State,County_Name,Attribute,Value\n01001,AL,A,Population_2020,5\n01001,AL,A,RUCC_2023,2\n")
    assert load_rucc(p) == {"01001": 2}


def test_download_pages_and_writes_meta(tmp_path, httpserver):
    pages = [
        {"features": [{"attributes": {"OBJECTID": i, "ID": str(i)}} for i in range(1, 2001)],
         "exceededTransferLimit": True},
        {"features": [{"attributes": {"OBJECTID": 2001, "ID": "2001"}}]},
    ]
    seen = []

    def handler(request):
        from werkzeug.wrappers import Response
        off = int(request.args["resultOffset"])
        seen.append((off, request.args["orderByFields"], request.headers["User-Agent"]))
        return Response(json.dumps(pages[0] if off == 0 else pages[1]), content_type="application/json")

    httpserver.expect_request("/query").respond_with_handler(handler)
    out = download_hifld(tmp_path / "hifld_raw.json", url=httpserver.url_for("/query"))
    assert [s[0] for s in seen] == [0, 2000]
    assert seen[0][1] == "OBJECTID" and seen[0][2].startswith("Mozilla/")
    assert len(json.loads(out.read_text())["features"]) == 2001
    meta = json.loads((tmp_path / "hifld_raw.meta.json").read_text())
    assert meta["record_count"] == 2001 and len(meta["sha256"]) == 64 and meta["url"].endswith("/query")


def test_load_rucc_accepts_windows_1252(tmp_path):
    p = tmp_path / "r.csv"
    p.write_bytes("FIPS,State,County_Name,Attribute,Value\n35013,NM,Doña Ana County,RUCC_2023,2\n".encode("cp1252"))
    assert load_rucc(p) == {"35013": 2}


def test_extended_shared_platforms():
    from noroshi.domains import is_shared_platform
    for d in ("wsha.org", "ny.gov", "va.gov", "business.site"):
        assert is_shared_platform(d)
    for d in ("waukeshacounty.gov", "brown.wi.us", "lynch.ne.us", "milwaukeecounty.org"):
        assert is_shared_platform(d)
    for d in ("ucnj.org", "ripley.ms", "namisacramento.org", "sjgov.org", "srnm.org", "smma.com",
              "spechospnjfdn.org", "shopaitribes.org", "alaweb.com", "brokenbow-ne.com",
              "myflfamilies.com", "wvdhhr.org", "scvrd.net"):
        assert is_shared_platform(d)
    # dedicated government-owned health system and university hospital domains stay in
    for d in ("ascension.org", "cookcountyhhs.org", "morrowcountyhealthdistrict.org", "umich.edu"):
        assert not is_shared_platform(d)


def test_beds_sum_sums_known_units_when_some_missing(tmp_path):
    raw = tmp_path / "r.json"
    raw.write_text(json.dumps({"features": [
        rec("a", "A", website="https://x.org", beds=40), rec("b", "B", website="https://x.org", beds=-999)]}))
    (tmp_path / "u.csv").write_text("FIPS,State,County_Name,Attribute,Value\n")
    _, d = build_hospital_frame(raw, tmp_path / "u.csv")
    assert d.loc[0, "beds_sum"] == 40


def test_audit_candidates(tmp_path):
    raw = tmp_path / "r.json"
    raw.write_text(json.dumps({"features": [
        rec("own", "Andalusia Health", website="https://andalusia.org/about"),    # token in domain
        rec("root", "Plain Hospital", website="https://portal.org/"),               # no path
        rec("bare", "Plain Hospital", website="portal2.org"),                       # no path
        rec("cand1", "Cornerstone Behavioral Hospital", website="https://sample-portal.org/cornerstone-x/"),
        rec("cand2", "Other Place", website="https://sample-portal.org/other"),
        rec("closed", "Closed One", status="CLOSED", website="https://sample-portal.org/closed"),
    ]}))
    (tmp_path / "u.csv").write_text("FIPS,State,County_Name,Attribute,Value\n")
    h, _ = build_hospital_frame(raw, tmp_path / "u.csv")
    c = audit_candidates(h)
    assert list(c.columns) == ["hifld_id", "name", "state", "website_raw", "domain", "n_units_on_domain"]
    assert list(c.hifld_id) == ["cand1", "cand2"]
    assert set(c.n_units_on_domain) == {2}
