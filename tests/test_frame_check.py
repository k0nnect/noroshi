"""A17: the frame check run by every frame build."""
import json
from pathlib import Path

import pandas as pd
import pytest

from noroshi.frames import DOMAIN_COLUMNS, FrameCheckError, check_domain_table

CHECKOUT = Path(__file__).resolve().parents[1]  # committed inputs are read from here


def table(*domains, sector="device"):
    return pd.DataFrame([{"sector": sector, "domain": d, "start_url": f"https://{d}/", "n_units": 1,
                          "beds_sum": "", "name_tokens": ""} for d in domains], columns=DOMAIN_COLUMNS)


def test_clean_table_passes():
    check_domain_table(table("philips.com", "pydicom.github.io", "va.gov"))


@pytest.mark.parametrize("bad", ["github.io", "co.uk", "gov", "blogspot.com", "sedo.com", "godaddy.com", "hugedomains.com"])
def test_suffix_or_parking_domain_fails(bad):
    with pytest.raises(FrameCheckError, match=bad.replace(".", r"\.")):
        check_domain_table(table("philips.com", bad))


def test_company_table_build_runs_the_check():
    from noroshi.resolve import to_domain_table

    resolved = pd.DataFrame({"key": ["a", "b"], "name": ["pydicom", "Philips"], "domain": ["github.io", "philips.com"],
                             "resolution": ["agreed", "agreed"]})
    with pytest.raises(FrameCheckError, match="github"):
        to_domain_table(resolved, "icsma")


def test_hospital_frame_build_runs_the_check(tmp_path):
    from noroshi.frames.hospitals import build_hospital_frame

    feats = [{"attributes": {"ID": "1", "NAME": "Parked Hospital", "STATE": "AL", "STATUS": "OPEN",
                             "WEBSITE": "https://www.sedo.com/parked", "OWNER": "NON-PROFIT",
                             "TYPE": "GENERAL ACUTE CARE", "BEDS": 5, "COUNTYFIPS": "01001"}}]
    (tmp_path / "raw.json").write_text(json.dumps({"features": feats}))
    (tmp_path / "rucc.csv").write_text("FIPS,State,County_Name,Attribute,Value\n01001,AL,A,RUCC_2023,2\n")
    with pytest.raises(FrameCheckError, match="sedo.com"):
        build_hospital_frame(tmp_path / "raw.json", tmp_path / "rucc.csv")


def test_federal_frame_build_runs_the_check(tmp_path):
    from noroshi.frames.federal import build_federal_frame

    (tmp_path / "f.csv").write_text("Domain name,Domain type,Organization name\n"
                                    "gov,Federal - Executive,Department of Health and Human Services\n")
    with pytest.raises(FrameCheckError, match="gov"):
        build_federal_frame(tmp_path / "f.csv")


# ---- rulings round, R1: federal portals are third parties for the hospital sector only

def test_third_party_domains_by_sector():
    from noroshi.domains import FEDERAL_PORTAL_DOMAINS, PARKING_DOMAINS, SHARED_PLATFORM_DOMAINS, third_party_domains

    assert FEDERAL_PORTAL_DOMAINS == {"va.gov", "tricare.mil", "ihs.gov", "navy.mil", "medicare.gov"}
    assert FEDERAL_PORTAL_DOMAINS <= SHARED_PLATFORM_DOMAINS
    assert third_party_domains("hospital") == SHARED_PLATFORM_DOMAINS | PARKING_DOMAINS
    for sector in ("federal", "device", "icsma", "ehr"):
        assert third_party_domains(sector) == (SHARED_PLATFORM_DOMAINS - FEDERAL_PORTAL_DOMAINS) | PARKING_DOMAINS


def test_frame_check_bans_third_parties_of_the_tables_sector():
    with pytest.raises(FrameCheckError, match=r"va\.gov"):
        check_domain_table(table("andalusia.org", "va.gov", sector="hospital"))
    check_domain_table(table("cdc.gov", "va.gov", "medicare.gov", "ihs.gov", sector="federal"))
    check_domain_table(table("ihs.gov", sector="ehr"))
    for sector, bad in (("device", "ny.gov"), ("federal", "facebook.com"), ("ehr", "sedo.com"), ("icsma", "business.site")):
        with pytest.raises(FrameCheckError, match=bad.replace(".", r"\.")):
            check_domain_table(table("philips.com", bad, sector=sector))


# ---- rulings round, R1 and R2: the committed tables

@pytest.mark.parametrize("sector", ["hospital", "federal", "device", "icsma", "ehr"])
def test_every_committed_domain_table_passes_the_check(sector):
    from noroshi import config

    check_domain_table(pd.read_csv(CHECKOUT / "data" / "frames" / f"{sector}_domains.csv", dtype=str,
                                   keep_default_na=False))


def test_committed_pass_files_resolve_pydicom_to_its_own_site():
    from noroshi import config
    from noroshi.resolve import reconcile

    passes = CHECKOUT / "data" / "frames" / "resolution_passes"
    got = reconcile(passes / "icsma_memory_a.jsonl", passes / "icsma_memory_b.jsonl", passes / "icsma_search.jsonl")
    assert got.set_index("key").loc["pydicom", "domain"] == "pydicom.github.io"


# ---- fix round 3, K11: every listed domain is its own registrable domain (the opt-out purge keys on it, H6)

@pytest.mark.parametrize("bad", ["med.x-gone.org", "www.philips.com", "Philips.com", "philips.com."])
def test_a_listed_domain_that_is_not_its_own_registrable_domain_fails(bad):
    with pytest.raises(FrameCheckError, match="not its own registrable domain"):
        check_domain_table(table("va.gov", bad))
