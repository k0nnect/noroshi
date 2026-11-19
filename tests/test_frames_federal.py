from noroshi.frames.federal import build_federal_frame

CSV = """Domain name,Domain type,Organization name,Suborganization name,City,State,Security contact email
cdc.gov,Federal - Executive,Department of Health and Human Services,CDC,Atlanta,GA,secret@cdc.gov
VA.gov,Federal - Executive,Department of Veterans Affairs,,Washington,DC,
uscourts.gov,Federal - Judicial,Department of Health and Human Services,,Washington,DC,
treasury.gov,Federal - Executive,Department of the Treasury,,Washington,DC,
nih.gov,Federal - Executive,Department of Health and Human Services,NIH,Bethesda,MD,
cdc.gov,Federal - Executive,Department of Health and Human Services,CDC,Atlanta,GA,
"""


def test_federal_frame_filters_org_and_type(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text(CSV)
    df = build_federal_frame(p)
    assert list(df.domain) == ["cdc.gov", "nih.gov", "va.gov"]
    assert list(df.columns) == ["sector", "domain", "start_url", "n_units", "beds_sum", "name_tokens"]
    assert set(df.sector) == {"federal"}
    assert set(df.n_units) == {1}
    assert (df.name_tokens == "").all() and df.beds_sum.isna().all()
    assert df.set_index("domain").loc["va.gov", "start_url"] == "https://va.gov/"


def test_federal_frame_never_carries_contact_email(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text(CSV)
    df = build_federal_frame(p)
    assert "secret@cdc.gov" not in df.to_csv()
