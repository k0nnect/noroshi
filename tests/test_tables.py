import csv
import json
import math

import pandas as pd

import synth
from noroshi.analysis import figures, tables
from noroshi.outcomes import derive_domains, derive_hospitals


def make(synthetic):
    store, frame, judg, reg, wb = synthetic
    d = derive_domains(store, frame, judg, reg, wb, synth.NOW)
    rows = []
    for i, (dom, beds, typ, owner, rural) in enumerate([
            ("good.org", "100", "GENERAL ACUTE CARE", "NON-PROFIT", "True"),
            ("good.org", "", "CRITICAL ACCESS", "GOVERNMENT - STATE", "False"),
            ("expired.org", "350", "PSYCHIATRIC", "PROPRIETARY", "False"),
            ("none.org", "40", "CHILDREN", "NOT AVAILABLE", ""),
            ("blocked.org", "80", "GENERAL ACUTE CARE", "NON-PROFIT", "True"),
            ("optout.org", "80", "GENERAL ACUTE CARE", "NON-PROFIT", "True"),
            ("unmeasured.org", "60", "OTHER", "NON-PROFIT", "True"),
            ("policyonly.org", "20", "REHABILITATION", "NON-PROFIT", "True")]):
        rows.append({"hifld_id": str(i), "name": "x", "state": "TX", "type": typ, "owner": owner, "beds": beds,
                     "rural": rural, "domain": dom, "exclusion": ""})
    h = derive_hospitals(pd.DataFrame(rows), d)
    return d, h


def test_shares_and_denominators(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    # hospital domains measured with a known main outcome: good, expired, root, secret, pass2, late, none,
    # policyonly, cnaonly, lenient, plat  (unmeasured.org has no outcome; blocked/down/optout/notcrawled excluded)
    assert n["hospital.domain.main.n"] == 11
    assert n["hospital.domain.main.k"] == 6  # good, expired, root, secret, pass2, late
    assert abs(n["hospital.domain.main.share"] - 6 / 11) < 1e-12
    assert n["hospital.domain.broad.n"] == 11  # unmeasured.org: no policy page, no listing, main unknown: empty
    assert n["hospital.domain.broad.k"] == 9  # six main + policyonly + cnaonly + plat
    assert n["hospital.domain.strict.k"] == 5
    # unmeasured.org has no robots.txt record, so its look for a policy page was incomplete (B3): not in policy.n
    assert n["hospital.domain.policy.n"] == 11 and n["hospital.domain.policy.k"] == 1
    assert n["federal.domain.main.k"] == 1 and n["federal.domain.main.n"] == 1
    assert n["device.domain.main.k"] == 0 and n["device.domain.main.n"] == 1
    assert n["icsma.domain.main.n"] == 0 and n["icsma.domain.main.share"] is None
    assert n["icsma.domain.main.ci_lo"] is None


def test_sensitivity_counts_unmeasured_as_absent(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    # frame minus optout.org and notcrawled.org: 14 hospital domains; blocked, down, unmeasured count as absent
    assert n["hospital.sens_domain.main.n"] == 14 and n["hospital.sens_domain.main.k"] == 6
    assert n["hospital.sens_domain.main.share"] < n["hospital.domain.main.share"]


def test_flow_counts(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["flow.hospital.optout"] == 1 and n["flow.hospital.not_crawled"] == 1
    assert n["flow.hospital.blocked"] == 1 and n["flow.hospital.unreachable"] == 1
    assert n["flow.hospital.measured"] == 12 and n["flow.hospital.browser"] == 2
    assert n["flow.hospital.frame"] == 16 and n["flow.all.frame"] == 18


def test_hospital_level_and_bed_weighted(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    # outcome known: good x2 (main), expired, none, policyonly, (unmeasured.org broad unknown but not in h);
    # blocked/optout hospitals are empty; unmeasured.org hospital: main empty, broad empty
    assert n["hospital.hospital.main.n"] == 5 and n["hospital.hospital.main.k"] == 3
    assert n["hospital.hospital.broad.n"] == 5 and n["hospital.hospital.broad.k"] == 4
    assert n["hospital.beds.broad.hospitals"] == 4 and n["hospital.beds.broad.excluded_unknown_beds"] == 1
    assert n["hospital.beds.broad.n"] == 100 + 350 + 40 + 20 and n["hospital.beds.broad.k"] == 100 + 350 + 20
    assert abs(n["hospital.beds.broad.share"] - 470 / 510) < 1e-12


def test_hospital_level_rows_have_no_interval(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    for prefix in ("hospital.hospital", "hospital.beds", "hospital.sens_hospital", "hospital.sens_beds"):
        assert not [k for k in n if k.startswith(prefix) and k.rsplit(".", 1)[1] in ("ci_lo", "ci_hi")]
    assert "hospital.domain.main.ci_lo" in n and "hospital.sens_domain.main.ci_hi" in n
    rows = list(csv.DictReader((tmp_path / "t" / "table2_shares.csv").read_text().splitlines()))
    assert all(r["ci_lo"] == "" and r["ci_hi"] == "" for r in rows if r["level"] in ("hospital", "beds"))
    assert any(r["ci_lo"] for r in rows if r["level"] == "domain")


def test_table3_descriptive_mode_three_parts(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    # (a) system size over hospital domains: 11 measured with a known broad outcome, good.org has 3 units
    assert n["hospital.char_domain.system_size.1.n"] == 10 and n["hospital.char_domain.system_size.1.k"] == 8
    assert n["hospital.char_domain.system_size.2_5.n"] == 1 and n["hospital.char_domain.system_size.2_5.k"] == 1
    assert n["hospital.char_domain.system_size.1.ci_lo"] is not None
    assert 0 <= n["hospital.char_domain.single_vs_multi.p"] <= 1
    assert "hospital.char_domain.type.psychiatric.n" not in n
    # (b) single-hospital domains: expired (psychiatric, broad), none (children, not), policyonly (rehab, broad)
    assert n["hospital.char_single.type.psychiatric.n"] == 1 and n["hospital.char_single.type.psychiatric.k"] == 1
    assert n["hospital.char_single.type.children.k"] == 0 and n["hospital.char_single.type.children.share"] == 0
    assert "hospital.char_single.type.children.ci_hi" in n
    assert n["hospital.char_single.owner.unknown.n"] == 1
    assert n["hospital.char_single.beds.under_100.n"] == 2 and n["hospital.char_single.beds.under_100.k"] == 1
    assert n["hospital.char_single.beds.300_plus.n"] == 1
    assert "hospital.char_single.system_size.1.n" not in n
    # (c) every hospital, inheriting its domain: good.org twice
    assert n["hospital.char_hospital.type.general_acute_care.n"] == 1
    assert n["hospital.char_hospital.type.critical_access.k"] == 1
    assert n["hospital.char_hospital.beds.unknown.n"] == 1 and n["hospital.char_hospital.rural.unknown.n"] == 1
    assert not [k for k in n if k.startswith("hospital.char_hospital") and k.endswith((".ci_lo", ".ci_hi"))]
    # the only p-value is the Fisher test
    assert [k for k in n if k.endswith(".p") and k.startswith("hospital.char")] == ["hospital.char_domain.single_vs_multi.p"]


def test_table3_columns_name_the_unit(synthetic, tmp_path):
    d, h = make(synthetic)
    tables.build_all(d, h, tmp_path / "t")
    rows = list(csv.DictReader((tmp_path / "t" / "table3_hospital_characteristics.csv").read_text().splitlines()))
    assert list(rows[0]) == ["part", "unit", "variable", "category", "units_measured", "units_broad", "share", "ci_lo",
                             "ci_hi", "p_value"]
    assert {r["part"] for r in rows} == {"a", "b", "c"}
    assert {r["unit"] for r in rows} == {"hospital domain", "single-hospital domain", "hospital"}
    assert all(r["ci_lo"] == "" and r["p_value"] == "" for r in rows if r["unit"] == "hospital")
    assert [r["variable"] for r in rows if r["p_value"]] == ["single_vs_multi"]


def _many_positive(n_pos=60, n_neg=40, beds=12_345):
    """A derived domain table and hospital table with many positive hospital domains and large bed counts."""
    from noroshi.outcomes import COLUMNS
    doms, hosp = [], []
    for i in range(n_pos + n_neg):
        pos = i < n_pos
        row = dict.fromkeys(COLUMNS)
        row.update(sector="hospital", domain=f"h{i}.org", n_units=1 if i % 3 else 4, status="measured",
                   ua_kind="identified", sectxt_class="valid" if pos else "absent", cmshpt_class="absent",
                   policy_page="no", main=pos, strict=pos, listing=False, broad=pos, contact_types="mailto" if pos else "",
                   expires_status="valid" if pos else "")
        doms.append(row)
        hosp.append({"hifld_id": str(i), "name": "x", "state": "TX", "type": "GENERAL ACUTE CARE",
                     "owner": "NON-PROFIT", "beds": str(beds), "rural": "True" if i % 2 else "False",
                     "domain": f"h{i}.org", "n_units": row["n_units"], "status": "measured", "main": pos,
                     "strict": pos, "policy_page": "no", "listing": False, "broad": pos, "registry_listing": False})
    return pd.DataFrame(doms, columns=COLUMNS), pd.DataFrame(hosp)


def test_table3_is_descriptive_whatever_the_number_of_positive_domains(tmp_path):
    import inspect
    d, h = _many_positive()
    assert "mode" not in inspect.signature(tables.build_all).parameters
    n = tables.build_all(d, h, tmp_path / "t")
    assert not [k for k in n if k.startswith("hospital.model") or k.startswith("hospital.char.")]
    rows = list(csv.DictReader((tmp_path / "t" / "table3_hospital_characteristics.csv").read_text().splitlines()))
    assert {r["part"] for r in rows} == {"a", "b", "c"}
    assert n["hospital.char_domain.system_size.1.k"] + n["hospital.char_domain.system_size.2_5.k"] == 60


def _md_rows(path):
    lines = path.read_text().splitlines()
    head = [c.strip() for c in lines[0].strip("|").split("|")]
    return [dict(zip(head, [c.strip() for c in line.strip("|").split("|")])) for line in lines[2:] if line.startswith("|")]


def test_markdown_shows_only_shares_as_percentages(tmp_path):
    d, h = _many_positive()
    tables.build_all(d, h, tmp_path / "t")
    beds = [r for r in _md_rows(tmp_path / "t" / "table2_shares.md") if r["level"] == "beds" and r["measure"] == "broad"][0]
    assert beds["n"] == str(100 * 12_345) and beds["k"] == str(60 * 12_345) and beds["share"] == "60.0%"
    dom = [r for r in _md_rows(tmp_path / "t" / "table2_shares.md") if r["level"] == "domain" and r["measure"] == "main"][0]
    assert dom["k"] == "60" and dom["n"] == "100" and dom["share"] == "60.0%" and dom["ci_lo"].endswith("%")
    paired = {r["stat"]: r["value"] for r in _md_rows(tmp_path / "t" / "table5_paired.md")}
    assert paired["domains"] == "100" and paired["main_only"] == "60" and paired["any_cms_k"] == "0"
    assert paired["any_cms_share"] == "0.0%" and paired["any_cms_ci_hi"].endswith("%")
    assert not paired["mcnemar_p"].endswith("%") and float(paired["mcnemar_p"]) < 1e-6
    t3 = _md_rows(tmp_path / "t" / "table3_hospital_characteristics.md")
    p = [r for r in t3 if r["variable"] == "single_vs_multi"][0]["p_value"]
    assert p and not p.endswith("%")
    for name in ("table1_flow", "table4_quality", "tableS1_sensitivity"):
        for r in _md_rows(tmp_path / "t" / f"{name}.md"):
            for col, v in r.items():
                assert not v.endswith("%") or col in ("share", "ci_lo", "ci_hi"), (name, col, v)


def test_paired_table(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    # both measured: good (main, cms), expired (main, cms invalid), root (main, cms), secret (main, no cms),
    # pass2, late, policyonly, cnaonly, none, lenient, plat (no main, no cms) -> unmeasured.org is out
    assert n["paired.n"] == 11
    assert n["paired.main_and_cms_valid"] == 2 and n["paired.main_only"] == 4
    assert n["paired.cms_valid_only"] == 0 and n["paired.neither"] == 5
    assert abs(n["paired.mcnemar_p"] - 0.125) < 1e-12
    assert n["paired.any_cms.k"] == 3


def test_quality_table(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["hospital.quality.contact_mailto.n"] == 6 and n["hospital.quality.contact_tel.k"] == 1
    assert n["hospital.quality.expires_expired.k"] == 1 and n["hospital.quality.expires_valid.k"] == 5
    assert n["hospital.quality.text_plain.k"] == 6
    assert n["hospital.quality.only_lenient_or_malformed.k"] == 1


def test_primary_share_excludes_unmeasured_and_sensitivity_includes(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["hospital.domain.main.n"] < n["hospital.sens_domain.main.n"]


import pytest


def test_numbers_json_matches_tables(synthetic, tmp_path):
    d, h = make(synthetic)
    out = tmp_path / "t"
    n = tables.build_all(d, h, out)
    tables.write_numbers(n, tmp_path / "numbers.json")
    loaded = json.loads((tmp_path / "numbers.json").read_text())
    cells = []
    for f in out.glob("*.csv"):
        for row in csv.DictReader(f.read_text().splitlines()):
            for v in row.values():
                try:
                    cells.append(float(v))
                except ValueError:
                    pass
    numeric = [v for v in loaded.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert all(any(c == v for v in numeric) for c in cells)  # every table number is in the dictionary
    assert all(v in cells for v in numeric)  # and every number in the dictionary is in a table
    assert (out / "table2_shares.md").exists() and (out / "tableS1_sensitivity.md").exists()


def test_empty_store_does_not_crash(tmp_path):
    from noroshi.outcomes import COLUMNS
    d = pd.DataFrame(columns=COLUMNS)
    h = pd.DataFrame(columns=["hifld_id", "name", "state", "type", "owner", "beds", "rural", "domain", "n_units",
                              "status", "main", "strict", "policy_page", "listing", "broad", "registry_listing"])
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["hospital.domain.main.n"] == 0 and n["hospital.domain.main.share"] is None
    assert n["hospital.hospital.broad.n"] == 0 and n["hospital.beds.broad.share"] is None
    assert n["paired.mcnemar_p"] is None


def test_figures(synthetic, tmp_path, capsys):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    made = figures.shares_by_sector(n, tmp_path / "f")
    assert all(p.exists() and p.stat().st_size > 0 for p in made) and {p.suffix for p in made} == {".png", ".pdf"}
    assert figures.first_seen_curves(d, tmp_path / "f") == []
    assert "skipped" in capsys.readouterr().out
    d.loc[d.domain == "good.org", "first_seen"] = "2020-05-01"
    d.loc[d.domain == "root.org", "first_seen"] = "2021-01-01"
    assert len(figures.first_seen_curves(d, tmp_path / "f")) == 2


def test_flow_counts_policy_unjudged(synthetic, tmp_path):
    store, frame, _, reg, wb = synthetic
    d = derive_domains(store, frame, tmp_path / "missing.jsonl", reg, wb, synth.NOW)
    h = derive_hospitals(pd.DataFrame([{"hifld_id": "1", "name": "x", "state": "TX", "type": "", "owner": "",
                                        "beds": "5", "rural": "", "domain": "good.org", "exclusion": ""}]), d)
    n = tables.build_all(d, h, tmp_path / "t")
    # policyonly.org (its page has no judgement) and unmeasured.org (no robots.txt record); blocked.org is not measured
    assert n["flow.hospital.policy_unjudged"] == 2
    assert n["hospital.domain.policy.n"] == 10


# ---- B11: in the sensitivity table a registry listing needs no crawl

def test_sensitivity_counts_a_registry_listing_of_a_blocked_or_unreachable_domain(synthetic, tmp_path):
    store, frame, judg, reg, wb = synthetic
    base = tables.build_all(*make(synthetic), tmp_path / "a")
    reg.cna |= {"blocked.org"}
    reg.diodb |= {"down.org"}
    d, h = make(synthetic)
    assert d.set_index("domain").loc["blocked.org", "registry_listing"] is True
    assert d.set_index("domain").loc["down.org", "registry_listing"] is True
    n = tables.build_all(d, h, tmp_path / "b")
    assert n["hospital.sens_domain.broad.k"] == base["hospital.sens_domain.broad.k"] + 2
    assert n["hospital.sens_domain.listing.k"] == base["hospital.sens_domain.listing.k"] + 2
    assert n["hospital.sens_hospital.broad.k"] == base["hospital.sens_hospital.broad.k"] + 1  # blocked.org's hospital
    assert n["hospital.domain.broad.k"] == base["hospital.domain.broad.k"]  # the primary table is unchanged
    assert n["hospital.domain.broad.n"] == base["hospital.domain.broad.n"]
    note = (tmp_path / "b" / "tableS1_sensitivity.md").read_text()
    assert "registry listing" in note and "blocked or unreachable" in note


# ---- B13: listed domains and final sites

def _with_dual(synthetic):
    """The synthetic table plus good.org listed again as a device maker (crawled once, as a hospital)."""
    store, frame, judg, reg, wb = synthetic
    dual = pd.concat([frame, pd.DataFrame([{"sector": "device", "domain": "good.org", "start_url": "https://good.org/",
                                            "n_units": "1", "beds_sum": "", "name_tokens": ""}])], ignore_index=True)
    return derive_domains(store, dual, judg, reg, wb, synth.NOW)


def test_flow_all_sectors_row_counts_unique_domains_and_states_the_overlap(synthetic, tmp_path):
    d = _with_dual(synthetic)
    _, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["flow.hospital.frame"] == 16 and n["flow.device.frame"] == 2
    assert n["flow.all.frame"] == 18 and n["flow.all.measured"] == 14  # 12 hospital, fed.gov, vendor.com: good.org once
    assert n["flow.all.multi_sector"] == 1 and n["flow.device.multi_sector"] == 1 and n["flow.hospital.multi_sector"] == 1
    assert n["flow.federal.multi_sector"] == 0


def test_sensitivity_counts_one_unit_per_final_site(synthetic, tmp_path):
    store, frame, judg, reg, wb = synthetic
    for alias in ("alias1.org", "alias2.org"):  # two listed domains that land on root.org's site
        synth.rec(store, alias, "homepage", synth.HOME, ctype="text/html", url=f"https://{alias}/",
                  final="https://www.root.org/")
        synth.rec(store, alias, "sectxt_wellknown", synth.GOOD.replace(b"{d}", b"root.org"),
                  url="https://www.root.org/.well-known/security.txt")
        store.mark_done(alias, 1, "measured")
    more = pd.concat([frame, pd.DataFrame([{"sector": "hospital", "domain": a, "start_url": f"https://{a}/",
                                            "n_units": "1", "beds_sum": "", "name_tokens": ""}
                                           for a in ("alias1.org", "alias2.org")])], ignore_index=True)
    d = derive_domains(store, more, judg, reg, wb, synth.NOW)
    assert set(d[d.domain.str.startswith("alias")].final_domain) == {"root.org"}
    _, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["hospital.sens_domain.main.n"] == 16 and n["hospital.sens_domain.main.k"] == 8
    # root.org and its two aliases are one final site; blocked, down and unmeasured each count as their own unit
    assert n["hospital.sens_final_site.main.n"] == 14 and n["hospital.sens_final_site.main.k"] == 6
    assert n["hospital.sens_final_site.main.ci_lo"] is not None
    rows = list(csv.DictReader((tmp_path / "t" / "tableS1_sensitivity.csv").read_text().splitlines()))
    assert {r["level"] for r in rows if r["sector"] == "hospital"} == {"domain", "final_site", "hospital", "beds"}
    assert "final_site" not in {r["level"] for r in csv.DictReader(
        (tmp_path / "t" / "table2_shares.csv").read_text().splitlines())}


# ---- B3, B15: the flow table's look reasons, cut paths, requests sent and 429 blocks

def test_flow_reports_look_reasons_cut_paths_requests_and_429_blocks(synthetic, tmp_path):
    store = synthetic[0]
    synth.rec(store, "down.org", "homepage", None, status=None, error="in_flight")  # sent, never completed
    d, h = make(synthetic)
    d.loc[d.domain == "blocked.org", "blocked_reason"] = "rate_limited"
    d.loc[d.domain == "none.org", ["policy_page", "policy_look"]] = ["unmeasured", "candidate_skipped;request_failed"]
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["flow.hospital.look_robots_unreadable"] == 1  # unmeasured.org: no robots.txt record
    assert n["flow.hospital.look_candidate_skipped"] == 1 and n["flow.all.look_candidate_skipped"] == 1
    assert n["flow.hospital.look_request_failed"] == 1
    assert n["flow.hospital.look_not_judged"] == 0
    # the synthetic store holds no conventional-path records: every measured hospital domain but unmeasured.org
    # (its robots.txt was never read, so no path was planned) has paths cut
    assert n["flow.hospital.probes_cut"] == 11
    assert n["flow.hospital.blocked_rate_limited"] == 1 and n["flow.hospital.blocked"] == 1
    sent = int(pd.to_numeric(d[d.sector == "hospital"].requests_sent).sum())
    assert n["flow.hospital.requests_sent"] == sent and sent == store.db.execute(
        "SELECT COUNT(*) FROM requests WHERE NOT reused AND domain IN (SELECT domain FROM requests "
        "WHERE sector = 'hospital')").fetchone()[0]
    assert n["flow.all.requests_sent"] == store.db.execute("SELECT COUNT(*) FROM requests WHERE NOT reused").fetchone()[0]


# ---- closing fixes, C4: requests sent to purged domains stay in the flow table

def test_flow_counts_requests_sent_to_purged_domains_in_the_all_sectors_row(synthetic, tmp_path):
    d, h = make(synthetic)
    n = tables.build_all(d, h, tmp_path / "t", requests_sent_purged=7)
    assert n["flow.all.requests_sent_purged"] == 7
    assert [k for k in n if k.endswith(".requests_sent_purged")] == ["flow.all.requests_sent_purged"]
    rows = {r["sector"]: r for r in csv.DictReader((tmp_path / "t" / "table1_flow.csv").read_text().splitlines())}
    assert rows["all"]["requests_sent_purged"] == "7" and rows["hospital"]["requests_sent_purged"] == ""
    note = (tmp_path / "t" / "table1_flow.md").read_text()
    assert "sent to domains that later opted out and whose records were deleted" in note


# ---- post-pilot Y4 and Y5: what part X added, and the tables the protocol promises

def _derived(rows, sector="hospital"):
    """A derived domain table from a few hand-written rows, and a hospital table to match."""
    from noroshi.outcomes import COLUMNS
    out, hosp = [], []
    for i, kw in enumerate(rows):
        r = dict.fromkeys(COLUMNS)
        r.update(sector=sector, domain=f"d{i}.org", n_units=1, status="measured", ua_kind="identified",
                 sectxt_class="absent", cmshpt_class="absent" if sector == "hospital" else "", policy_page="no",
                 main=False, strict=False, listing=False, broad=False, registry_listing=False)
        r.update(kw)
        out.append(r)
        hosp.append({"hifld_id": str(i), "name": "x", "state": "TX", "type": "GENERAL ACUTE CARE", "owner": "NON-PROFIT",
                     "beds": "10", "rural": "True", "domain": r["domain"], "n_units": 1, "status": r["status"],
                     "main": r["main"], "strict": r["strict"], "policy_page": r["policy_page"], "listing": r["listing"],
                     "broad": r["broad"], "registry_listing": r["registry_listing"]})
    return pd.DataFrame(out, columns=COLUMNS), pd.DataFrame(hosp)


def test_a_forbidden_file_counts_as_no_file_and_has_its_own_flow_count(tmp_path):
    d, h = _derived([
        dict(sectxt_class="forbidden", cmshpt_class="forbidden", file_forbidden=True),
        dict(sectxt_class="valid", main=True, strict=True, broad=True, cmshpt_class="present_valid",
             file_forbidden=False),
        dict(sectxt_class="absent", cmshpt_class="present_valid", file_forbidden=False),
        dict(sectxt_class="valid_with_problems", main=True, broad=True, cmshpt_class="forbidden", file_forbidden=True),
        dict(status="blocked", sectxt_class="unmeasured", main=None, strict=None, broad=None, policy_page="unmeasured",
             cmshpt_class="unmeasured", file_forbidden=None)])
    n = tables.build_all(d, h, tmp_path / "t")
    assert n["flow.hospital.file_forbidden"] == 2 and n["flow.all.file_forbidden"] == 2
    assert (n["hospital.domain.main.k"], n["hospital.domain.main.n"]) == (2, 4)
    assert (n["hospital.domain.strict.k"], n["hospital.domain.strict.n"]) == (1, 4)
    assert (n["paired.n"], n["paired.main_and_cms_valid"], n["paired.main_only"], n["paired.cms_valid_only"],
            n["paired.neither"]) == (4, 1, 1, 1, 1)


def test_the_flow_table_counts_switched_sites_apart_from_sites_that_saw_the_browser_signature(tmp_path):
    d, h = _derived([dict(ua_kind="browser", site_switched=True), dict(ua_kind="browser", site_switched=False),
                     dict(ua_kind="identified", site_switched=False),
                     dict(status="blocked", ua_kind="browser", site_switched=True, sectxt_class="unmeasured",
                          main=None, strict=None, broad=None, policy_page="unmeasured")])
    n = tables.build_all(d, h, tmp_path / "t")
    assert (n["flow.hospital.browser"], n["flow.hospital.site_switched"]) == (2, 1)


def test_the_flow_table_has_a_column_for_an_unreadable_homepage(tmp_path):
    d, h = _derived([dict(policy_page="unmeasured", policy_look="homepage_unreadable", broad=None),
                     dict(policy_page="unmeasured", policy_look="homepage_truncated;homepage_unreadable", broad=None),
                     dict()])
    n = tables.build_all(d, h, tmp_path / "t")
    assert (n["flow.hospital.look_homepage_unreadable"], n["flow.hospital.look_homepage_truncated"]) == (2, 1)
    assert "look_homepage_unreadable" in (tmp_path / "t" / "table1_flow.csv").read_text().splitlines()[0]


def _verdict(inv, con, safe):
    return {"invites_reports": inv, "contact_method": con, "safe_harbor": safe}


def test_the_agreement_table_gives_agreement_and_kappa_for_each_judge_question_and_for_the_identity_raters(tmp_path):
    """T1: over pages (texts) both judges answered and domains both raters answered, before the tie-break."""
    d, h = _derived([dict(identity="own", identity_page_sha256="k0"), dict(identity="own", identity_page_sha256="k1"),
                     dict(identity="other", identity_page_sha256="k2", status="mismatch", sectxt_class="unmeasured",
                          main=None),
                     dict(identity="unclear", identity_page_sha256="k3", status="mismatch", sectxt_class="unmeasured",
                          main=None),
                     dict(status="optout", identity="")])
    y, n_ = _verdict(True, True, True), _verdict(False, False, False)
    judged = [  # (a, b) per distinct text; the copy of t1 and the opted-out domain's page are not counted again
        {"domain": "d0.org", "text_sha256": "t1", "a": y, "b": y, "final": y},
        {"domain": "d1.org", "text_sha256": "t1", "a": y, "b": y, "final": y},
        {"domain": "d0.org", "text_sha256": "t2", "a": y, "b": _verdict(True, False, False), "final": n_},
        {"domain": "d1.org", "text_sha256": "t3", "a": n_, "b": n_, "final": n_},
        {"domain": "d1.org", "text_sha256": "t4", "a": _verdict(True, True, False), "b": y, "final": y},
        {"domain": "d4.org", "text_sha256": "t5", "a": y, "b": n_, "final": n_},
        {"domain": "d1.org", "text_sha256": "t6", "a": None, "b": None, "final": None, "prefilter": False},
    ]
    rater = lambda s: {"model": "m", "site": s, "reason": "r"}
    decided = [  # d0 was decided for an older homepage too; derive used k0
        {"domain": "d0.org", "page_sha256": "old", "a": rater("own"), "b": rater("other"), "site": "other"},
        {"domain": "d0.org", "page_sha256": "k0", "a": rater("own"), "b": rater("own"), "site": "own"},
        {"domain": "d1.org", "page_sha256": "k1", "a": rater("own"), "b": rater("own"), "site": "own"},
        {"domain": "d2.org", "page_sha256": "k2", "a": rater("other"), "b": rater("other"), "site": "other"},
        {"domain": "d3.org", "page_sha256": "k3", "a": rater("unclear"), "b": rater("other"), "site": "unclear"},
        {"domain": "d4.org", "page_sha256": "k4", "a": rater("own"), "b": rater("own"), "site": "own"}]
    n = tables.build_all(d, h, tmp_path / "t", judgements=judged, identity=decided)
    # t1 to t4 (t1 once; t5 is the opted-out domain's; t6 was screened out): invites_reports agree on all four,
    # contact_method on three (t2 differs), safe_harbor on two (t2 and t4 differ)
    assert (n["agreement.judge.invites_reports.n"], n["agreement.judge.invites_reports.agree"]) == (4, 4)
    assert n["agreement.judge.invites_reports.kappa"] == 1.0 and n["agreement.judge.invites_reports.share"] == 1.0
    assert (n["agreement.judge.contact_method.agree"], n["agreement.judge.safe_harbor.agree"]) == (3, 2)
    # the decision derive used for each decided domain (d4 opted out): own/own, own/own, other/other, unclear/other
    assert (n["agreement.identity.site.n"], n["agreement.identity.site.agree"]) == (4, 3)
    assert abs(n["agreement.identity.site.kappa"] - 0.6) < 1e-9
    rows = list(csv.DictReader((tmp_path / "t" / "tableS2_agreement.csv").read_text().splitlines()))
    assert [(r["check"], r["question"]) for r in rows] == [("judge", "invites_reports"), ("judge", "contact_method"),
                                                          ("judge", "safe_harbor"), ("identity", "site")]


def test_kappa_is_zero_with_a_note_when_one_rater_gives_the_same_answer_every_time(tmp_path):
    """T1, and audit C1: the old prompt made one judge answer yes to every page."""
    d, h = _derived([dict()])
    y, no = _verdict(True, True, False), _verdict(False, False, False)
    judged = [{"domain": "d0.org", "text_sha256": f"t{i}", "a": y, "b": y if i < 3 else no, "final": y}
              for i in range(5)]
    n = tables.build_all(d, h, tmp_path / "t", judgements=judged, identity=[])
    assert n["agreement.judge.invites_reports.kappa"] == 0 and n["agreement.judge.invites_reports.agree"] == 3
    rows = {r["question"]: r for r in csv.DictReader((tmp_path / "t" / "tableS2_agreement.csv").read_text().splitlines())}
    assert rows["invites_reports"]["note"] == "kappa is 0 because one rater gave the same answer every time"
    # fix round 1, G2: both said false every time: kappa is not defined, the cell is empty, agreement is shown
    assert n["agreement.judge.safe_harbor.kappa"] is None and n["agreement.judge.safe_harbor.share"] == 1.0
    assert rows["safe_harbor"]["kappa"] == "" and rows["safe_harbor"]["agree"] == "5"
    assert rows["safe_harbor"]["note"] == "kappa is not defined because both raters gave the same answer every time"
    assert n["agreement.identity.site.n"] == 0 and n["agreement.identity.site.kappa"] is None


def test_the_flow_table_counts_disagreeing_passes_offdomain_domains_archived_redirects_and_mismatch_reasons(tmp_path):
    """T2, T3, T5."""
    d, h = _derived([dict(passes_disagree=True, redirected_offdomain=True), dict(passes_disagree=False),
                     dict(redirected_offdomain=True), dict(redirected_offdomain=False),
                     dict(status="unreachable", archive_redirect_target="newsite.org", sectxt_class="unmeasured",
                          main=None),
                     dict(status="unreachable", archive_redirect_target="", sectxt_class="unmeasured", main=None),
                     dict(status="mismatch", mismatch_reason="crawl_rule", main=None),
                     dict(status="mismatch", mismatch_reason="identity_other", main=None),
                     dict(status="mismatch", mismatch_reason="identity_other", main=None),
                     dict(status="mismatch", mismatch_reason="identity_unclear", main=None)])
    n = tables.build_all(d, h, tmp_path / "t")
    assert (n["flow.hospital.passes_disagree"], n["flow.hospital.redirected_offdomain"]) == (1, 2)
    assert n["flow.hospital.unreachable_archive_redirect"] == 1
    assert "flow.federal.unreachable_archive_redirect" not in n
    assert (n["flow.hospital.mismatch"], n["flow.hospital.mismatch_crawl_rule"], n["flow.hospital.mismatch_identity_other"],
            n["flow.hospital.mismatch_identity_unclear"]) == (4, 1, 2, 1)


def test_the_quality_table_gives_the_safe_harbor_share_among_policy_pages(tmp_path):
    """T4."""
    d, h = _derived([dict(policy_page="yes", safe_harbor="yes", broad=True),
                     dict(policy_page="yes", safe_harbor="no", broad=True),
                     dict(policy_page="yes", safe_harbor="no", broad=True), dict(policy_page="no")])
    n = tables.build_all(d, h, tmp_path / "t")
    p = "hospital.quality.policy_safe_harbor"
    assert (n[f"{p}.k"], n[f"{p}.n"]) == (1, 3) and 0 < n[f"{p}.ci_lo"] < 1 / 3 < n[f"{p}.ci_hi"] < 1
    assert "policy_safe_harbor" in (tmp_path / "t" / "table4_quality.csv").read_text()


def test_the_federal_control_is_reported_per_listed_domain_and_per_distinct_final_site(tmp_path):
    """Audit M9: 8 of 24 measured federal rows land on a site that is itself in the federal frame."""
    rows = [dict(broad=True, final_domain="samhsa.gov"), dict(broad=True, final_domain="samhsa.gov"),
            dict(broad=False, final_domain="samhsa.gov"), dict(broad=True, final_domain="nih.gov"),
            dict(broad=False, final_domain="healthcare.gov"), dict(broad=None, final_domain="x.gov"),
            dict(status="unreachable", broad=None, final_domain="")]
    d, h = _derived(rows, sector="federal")
    n = tables.build_all(d, h, tmp_path / "t")
    assert (n["federal.control.listed_domain.broad.k"], n["federal.control.listed_domain.broad.n"]) == (3, 5)
    assert (n["federal.control.final_site.broad.k"], n["federal.control.final_site.broad.n"]) == (2, 3)
    assert n["federal.control.listed_domain.broad.share"] == n["federal.domain.broad.share"]
    assert (tmp_path / "t" / "tableS3_federal_control.csv").exists()
