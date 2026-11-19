import json

import pandas as pd

from noroshi.frames.companies import (
    load_ehr_developers, load_fda_ai_list, load_icsma_vendors, normalize_company,
)


def test_normalize_company():
    assert normalize_company("Siemens Healthcare GmbH") == normalize_company("SIEMENS HEALTHCARE, GMBH.")
    assert normalize_company("Aidoc Medical, Ltd.") == "aidoc medical"
    assert normalize_company("Adas3D Medical S.L.") == "adas3d medical"
    assert normalize_company("A & B Inc.") == "a and b"


def test_fda_list_groups_by_key(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text(
        "Date of Final Decision,Submission Number,Device,Company,Panel (Lead),Primary Product Code\n"
        '06/29/2026,K1,A,"Aidoc Medical, Ltd.",Radiology,QIH\n'
        "06/29/2026,K2,B,AIDOC MEDICAL LTD,Radiology,QIH\n"
        "06/29/2026,K3,C,Philips,Radiology,QIH\n"
        "06/29/2026,K4,D,,Radiology,QIH\n"
    )
    df = load_fda_ai_list(p)
    assert list(df.columns) == ["key", "name", "n_devices"]
    assert df.set_index("key").n_devices.to_dict() == {"aidoc medical": 2, "philips": 1}


def _csaf(path, tid, vendors):
    branches = [{"category": "vendor", "name": v, "branches": []} for v in vendors]
    nested = [{"category": "vendor", "name": "Nested", "branches": []}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "document": {"tracking": {"id": tid}},
        "product_tree": {"branches": [{"category": "product_family", "name": "x", "branches": nested}] + branches},
    }))


def test_icsma_vendor_extraction(tmp_path):
    _csaf(tmp_path / "2026" / "icsma-26-001-01.json", "ICSMA-26-001-01", ["Acme Inc."])
    _csaf(tmp_path / "2026" / "icsma-26-002-01.json", "ICSMA-26-002-01", ["ACME, Inc", "Beta Corp"])
    _csaf(tmp_path / "2026" / "icsa-26-003-01.json", "ICSA-26-003-01", ["Gamma"])
    (tmp_path / "2026" / "broken.json").write_text("{")
    df = load_icsma_vendors(tmp_path)
    assert list(df.columns) == ["key", "name", "n_advisories"]
    d = df.set_index("key").n_advisories.to_dict()
    assert d == {"acme": 2, "beta": 1, "nested": 2}


def test_ehr_developers_distinct_nonempty(tmp_path):
    p = tmp_path / "l.csv"
    pd.DataFrame({"api_developer_name": ["Epic Systems Corporation", "Epic Systems Corp.", "", None, "Cerner"],
                  "other": range(5)}).to_csv(p, index=False)
    df = load_ehr_developers(p)
    assert list(df.columns) == ["key", "name", "website_raw"]
    assert sorted(df.key) == ["cerner", "epic systems"]
    assert (df.website_raw == "").all()
