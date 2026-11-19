import json

import pandas as pd

import synth
from noroshi.discover import PROBE_PATHS
from noroshi.outcomes import derive_domains, derive_hospitals, wayback_inputs


def derive(synthetic, **kw):
    store, frame, judg, reg, wb = synthetic
    return derive_domains(store, frame, judg, reg, wb, synth.NOW)


def idx(synthetic):
    return derive(synthetic).set_index("domain")


def test_blocked_domain_is_unmeasured_not_absent(synthetic):
    d = idx(synthetic)
    assert d.loc["blocked.org", "status"] == "blocked" and d.loc["blocked.org", "main"] is None
    assert d.loc["blocked.org", "broad"] is None and d.loc["blocked.org", "policy_page"] == "unmeasured"
    assert d.loc["down.org", "status"] == "unreachable" and d.loc["down.org", "main"] is None


def test_main_strict_broad(synthetic):
    d = idx(synthetic)
    assert d.loc["expired.org", "main"] and not d.loc["expired.org", "strict"]
    assert d.loc["expired.org", "sectxt_class"] == "valid_with_problems"
    assert d.loc["good.org", "strict"] and d.loc["good.org", "broad"]
    assert d.loc["policyonly.org", "broad"] and not d.loc["policyonly.org", "main"]
    assert d.loc["policyonly.org", "policy_page"] == "yes" and d.loc["policyonly.org", "safe_harbor"] == "yes"
    assert d.loc["cnaonly.com", "broad"] and d.loc["cnaonly.com", "cna"]
    assert d.loc["none.org", "broad"] is False and d.loc["none.org", "main"] is False
    assert d.loc["none.org", "policy_page"] == "no" and d.loc["none.org", "safe_harbor"] == ""
    # its unreadable PDF row has no stored record behind it, so it counts for nothing (fix round 1, G5)
    assert d.loc["none.org", "policy_pages_unjudged"] == 0


def test_missing_record_is_unmeasured_not_absent(synthetic):
    d = idx(synthetic)
    assert d.loc["unmeasured.org", "status"] == "measured"
    assert d.loc["unmeasured.org", "sectxt_class"] == "unmeasured"
    assert d.loc["unmeasured.org", "main"] is None and d.loc["unmeasured.org", "strict"] is None
    assert d.loc["unmeasured.org", "broad"] is None
    assert d.loc["unmeasured.org", "cmshpt_class"] == "unmeasured"
    assert d.loc["none.org", "sectxt_class"] == "absent"


def test_pass2_record_replaces_pass1(synthetic):
    d = idx(synthetic)
    assert d.loc["pass2.org", "main"] and d.loc["pass2.org", "ua_kind"] == "browser"
    assert d.loc["late.org", "status"] == "measured" and d.loc["late.org", "main"]


def test_root_location_used_when_wellknown_absent(synthetic):
    d = idx(synthetic)
    assert d.loc["root.org", "sectxt_location"] == "root" and d.loc["root.org", "main"]
    assert d.loc["good.org", "sectxt_location"] == "well-known"


def test_domain_status_and_not_crawled(synthetic):
    d = idx(synthetic)
    assert d.loc["notcrawled.org", "status"] == "not_crawled" and d.loc["notcrawled.org", "main"] is None
    assert d.loc["optout.org", "status"] == "optout" and d.loc["optout.org", "main"] is None


def test_extra_columns(synthetic):
    d = idx(synthetic)
    assert d.loc["good.org", "sectxt_text_plain"]
    assert d.loc["lenient.org", "lenient_contact"] and d.loc["lenient.org", "n_malformed_contacts"] == 1
    assert d.loc["lenient.org", "main"] is False and d.loc["lenient.org", "contact_types"] == ""
    assert d.loc["secret.org", "contact_types"] == "mailto;tel"
    assert d.loc["good.org", "cmshpt_class"] == "present_valid" and d.loc["expired.org", "cmshpt_class"] == "present_invalid"
    assert d.loc["fed.gov", "cmshpt_class"] == ""
    assert d.loc["good.org", "redirected_offdomain"] is False
    assert d.loc["good.org", "n_units"] == 3 and d.loc["good.org", "beds_sum"] == 300


def test_redirected_offdomain(tmp_path):
    store, frame, judg, reg = synth.build(tmp_path)
    synth.rec(store, "good.org", "homepage", synth.HOME, ctype="text/html", pass_no=2,
              url="https://good.org/", final="https://www.other-site.com/home")
    store.mark_done("good.org", 2, "measured")
    d = derive_domains(store, frame, judg, reg, tmp_path / "w.jsonl", synth.NOW).set_index("domain")
    assert d.loc["good.org", "redirected_offdomain"] is True
    store.close()


def test_cancelled_record_is_ignored(tmp_path):
    store, frame, judg, reg = synth.build(tmp_path)
    synth.rec(store, "good.org", "sectxt_wellknown", None, status=None, error="cancelled",
              url="https://good.org/.well-known/security.txt")
    d = derive_domains(store, frame, judg, reg, tmp_path / "w.jsonl", synth.NOW).set_index("domain")
    assert d.loc["good.org", "main"] is True
    store.close()


def test_platform_links_only_when_theirs(synthetic):
    d = idx(synthetic)
    assert d.loc["plat.org", "platforms"] == "hackerone;intigriti"  # bugcrowd link to another company is not
    assert d.loc["plat.org", "broad"] is True


def test_registry_platform_hit_counts(tmp_path):
    store, frame, judg, reg = synth.build(tmp_path)
    reg.platforms["bugcrowd"] = {"plat.org"}
    d = derive_domains(store, frame, judg, reg, tmp_path / "w.jsonl", synth.NOW).set_index("domain")
    assert d.loc["plat.org", "platforms"] == "bugcrowd;hackerone;intigriti"
    store.close()


def test_contact_values_never_in_output(synthetic):
    csv = derive(synthetic).to_csv()
    assert "secret-contact@" not in csv and "hidden.example" not in csv and "555" not in csv


def test_first_seen_from_wayback_file_and_missing_file(synthetic):
    store, frame, judg, reg, wb = synthetic
    assert (derive(synthetic)["first_seen"] == "").all()  # the file does not exist yet
    wb.write_text("".join(json.dumps(r) + "\n" for r in [
        {"domain": "good.org", "host": "good.org", "path": "/.well-known/security.txt", "first_seen": "2020-05-01"},
        {"domain": "root.org", "host": "root.org", "path": "/security.txt", "first_seen": None}]))
    d = idx(synthetic)
    assert d.loc["good.org", "first_seen"] == "2020-05-01" and d.loc["root.org", "first_seen"] == ""


def test_wayback_inputs(synthetic):
    d = derive(synthetic)
    first, redirects = wayback_inputs(d)
    assert set(first.columns) == {"domain", "host", "path"}
    assert set(first.domain) == {"good.org", "expired.org", "root.org", "secret.org", "pass2.org", "late.org",
                                 "fed.gov"}
    assert first.set_index("domain").loc["root.org", "path"] == "/security.txt"
    assert first.set_index("domain").loc["good.org", "path"] == "/.well-known/security.txt"
    assert list(redirects.columns) == ["domain", "host"] and list(redirects.domain) == ["down.org"]


def test_derive_hospitals_inherits_domain_outcome(synthetic):
    d = derive(synthetic)
    h = pd.DataFrame([
        {"hifld_id": "1", "name": "A", "state": "TX", "type": "GENERAL ACUTE CARE", "owner": "NON-PROFIT",
         "beds": "100", "rural": "True", "domain": "good.org", "exclusion": ""},
        {"hifld_id": "2", "name": "B", "state": "TX", "type": "CRITICAL ACCESS", "owner": "NON-PROFIT",
         "beds": "", "rural": "False", "domain": "good.org", "exclusion": ""},
        {"hifld_id": "3", "name": "C", "state": "TX", "type": "CRITICAL ACCESS", "owner": "NON-PROFIT",
         "beds": "5", "rural": "False", "domain": "", "exclusion": "no_website"},
    ])
    out = derive_hospitals(h, d)
    assert list(out.hifld_id) == ["1", "2"] and out.main.tolist() == [True, True]
    assert (out.n_units == 3).all()


def _no_judgements(synthetic, tmp_path):
    store, frame, _, reg, wb = synthetic
    return derive_domains(store, frame, tmp_path / "missing.jsonl", reg, wb, synth.NOW).set_index("domain")


def test_unjudged_stored_page_makes_policy_page_unmeasured(synthetic, tmp_path):
    d = _no_judgements(synthetic, tmp_path)
    assert d.loc["policyonly.org", "policy_page"] == "unmeasured"
    assert d.loc["policyonly.org", "policy_pages_missing_judgement"] == 1
    assert d.loc["policyonly.org", "broad"] is None


def test_main_outcome_keeps_broad_true_when_pages_unjudged(synthetic, tmp_path):
    store = synthetic[0]
    synth.rec(store, "good.org", "candidate", b"<p>vulnerability disclosure</p>", ctype="text/html")
    d = _no_judgements(synthetic, tmp_path)
    assert d.loc["good.org", "policy_page"] == "unmeasured" and d.loc["good.org", "broad"] is True


def test_no_stored_pages_means_no_policy_page(synthetic, tmp_path):
    d = _no_judgements(synthetic, tmp_path)
    assert d.loc["none.org", "policy_page"] == "no" and d.loc["none.org", "policy_pages_missing_judgement"] == 0
    assert d.loc["none.org", "broad"] is False
    assert d.loc["blocked.org", "policy_page"] == "unmeasured"


def _mini(tmp_path, build):
    """A fresh store with one hospital domain `t.org`, set up by build(store); returns its derived row."""
    store = synth.Store(tmp_path / "m.sqlite")
    build(store)
    frame = pd.DataFrame([{"sector": "hospital", "domain": "t.org", "start_url": "https://t.org/", "n_units": "1",
                           "beds_sum": "5", "name_tokens": ""}])
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    out = derive_domains(store, frame, tmp_path / "none.jsonl", reg, tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc["t.org"]


def _wk(store, body, status=200, **kw):
    synth.rec(store, "t.org", "sectxt_wellknown", body, status=status, url="https://t.org/.well-known/security.txt",
              **kw)


def _root(store, body, status=200, **kw):
    synth.rec(store, "t.org", "sectxt_root", body, status=status, url="https://t.org/security.txt", **kw)


def _home(store, **kw):
    synth.rec(store, "t.org", "homepage", synth.HOME, ctype="text/html", url="https://t.org/", **kw)


def test_unmeasured_wellknown_with_absent_root_is_unmeasured(tmp_path):
    def build(s):
        _home(s)
        _wk(s, None, status=None, error="timeout")
        _root(s, None, status=404)
        s.mark_done("t.org", 1, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "unmeasured" and r.main is None and r.sectxt_location == ""


def test_absent_wellknown_with_unmeasured_root_is_unmeasured(tmp_path):
    def build(s):
        _home(s)
        _wk(s, None, status=404)
        _root(s, None, status=None, error="timeout")
        s.mark_done("t.org", 1, "measured")
    assert _mini(tmp_path, build).sectxt_class == "unmeasured"


def test_unmeasured_wellknown_with_html_root_is_unmeasured(tmp_path):
    def build(s):
        _home(s)
        _wk(s, None, status=None, error="timeout")
        _root(s, b"<html>not found</html>", ctype="text/html")
        s.mark_done("t.org", 1, "measured")
    assert _mini(tmp_path, build).sectxt_class == "unmeasured"


def test_valid_file_survives_failed_pass2_attempts(tmp_path):
    """The reviewer's probe: pass 1 valid well-known, cms-hpt 503; pass 2 well-known refused, root 404."""
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        synth.rec(s, "t.org", "cmshpt", None, status=503, url="https://t.org/cms-hpt.txt")
        s.mark_done("t.org", 1, "measured")
        _home(s, pass_no=2)
        _wk(s, b"slow down", status=429, blocked=True, ctype="text/html", pass_no=2)
        _root(s, None, status=404, pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "valid" and r.main is True and r.sectxt_location == "well-known"


def test_later_error_does_not_erase_earlier_measurement(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        s.mark_done("t.org", 1, "measured")
        _wk(s, None, status=None, error="timeout", pass_no=2)
        s.mark_done("t.org", 2, "measured")
    assert _mini(tmp_path, build).main is True


def test_invalid_root_file_beats_absent_wellknown_and_gives_its_details(tmp_path):
    def build(s):
        _home(s)
        _wk(s, None, status=404)
        _root(s, synth.LENIENT)
        s.mark_done("t.org", 1, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "invalid" and r.sectxt_location == "root" and r.main is False
    assert r.lenient_contact is True and r.n_malformed_contacts == 1


def test_valid_preferred_over_valid_with_problems_and_wellknown_over_root(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.EXPIRED)
        _root(s, synth.GOOD.replace(b"{d}", b"t.org"))
        s.mark_done("t.org", 1, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "valid" and r.sectxt_location == "root" and r.strict is True


def test_run_time_does_not_change_expiry(synthetic):
    from datetime import datetime, timezone
    store, frame, judg, reg, wb = synthetic
    a = derive_domains(store, frame, judg, reg, wb, synth.NOW)
    b = derive_domains(store, frame, judg, reg, wb, datetime(2031, 6, 1, tzinfo=timezone.utc))
    cols = ["domain", "sectxt_class", "strict", "main", "expires_status"]
    assert a[cols].equals(b[cols]) and a.set_index("domain").loc["good.org", "strict"] is True


def test_malformed_policy_url_does_not_crash(tmp_path):
    def build(s):
        _home(s)
        _wk(s, b"Contact: mailto:a@t.org\nExpires: 2030-01-01T00:00:00Z\nPolicy: https://[badpolicy.org/vdp\n")
        s.mark_done("t.org", 1, "measured")
    assert _mini(tmp_path, build).main is True


def _cms(store, body, status=200, **kw):
    synth.rec(store, "t.org", "cmshpt", body, status=status, url="https://t.org/cms-hpt.txt", **kw)


def test_later_5xx_does_not_erase_earlier_measurement(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        _cms(s, synth.CMS_OK)
        s.mark_done("t.org", 1, "measured")
        _home(s, pass_no=2)
        _wk(s, b"down", status=503, ctype="text/html", pass_no=2)
        _root(s, None, status=404, pass_no=2)
        _cms(s, b"down", status=500, ctype="text/html", pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "valid" and r.main is True and r.cmshpt_class == "present_valid"


def test_root_file_survives_later_502(tmp_path):
    def build(s):
        _home(s)
        _wk(s, None, status=404)
        _root(s, synth.GOOD.replace(b"{d}", b"t.org"))
        s.mark_done("t.org", 1, "measured")
        _wk(s, None, status=404, pass_no=2)
        _root(s, b"bad gateway", status=502, ctype="text/html", pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "valid" and r.sectxt_location == "root"


def test_only_5xx_everywhere_is_still_unmeasured(tmp_path):
    def build(s):
        _home(s)
        _wk(s, b"down", status=503, ctype="text/html")
        _root(s, None, status=404)
        s.mark_done("t.org", 1, "measured")
    assert _mini(tmp_path, build).sectxt_class == "unmeasured"


def test_pass1_measured_pass2_blocked_stays_measured(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        s.mark_done("t.org", 1, "measured")
        synth.rec(s, "t.org", "homepage", b"no", status=403, ctype="text/html", url="https://t.org/",
                  blocked=True, pass_no=2)
        s.mark_done("t.org", 2, "blocked")
    r = _mini(tmp_path, build)
    assert r.status == "measured" and r.main is True


def test_pass1_unreachable_pass2_measured(tmp_path):
    def build(s):
        synth.rec(s, "t.org", "homepage", None, status=None, error="timeout", url="https://t.org/")
        s.mark_done("t.org", 1, "unreachable")
        _home(s, pass_no=2)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"), pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _mini(tmp_path, build)
    assert r.status == "measured" and r.main is True


def test_optout_in_any_pass_wins(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        s.mark_done("t.org", 1, "measured")
        s.mark_done("t.org", 2, "optout")
    r = _mini(tmp_path, build)
    assert r.status == "optout" and r.main is None and r.broad is None and r.policy_page == "unmeasured"


def test_latest_pass_status_when_none_measured(tmp_path):
    def build(s):
        synth.rec(s, "t.org", "homepage", None, status=None, error="timeout", url="https://t.org/")
        s.mark_done("t.org", 1, "unreachable")
        synth.rec(s, "t.org", "homepage", b"no", status=403, ctype="text/html", url="https://t.org/",
                  blocked=True, pass_no=2)
        s.mark_done("t.org", 2, "blocked")
    assert _mini(tmp_path, build).status == "blocked"


def test_platform_name_strips_trailing_dot():
    from noroshi.outcomes import _platform_name
    assert _platform_name("https://hackerone.com./t") == "hackerone"


# ---- B15: one suffix extractor, and every not-an-outcome label ignored

def test_outcomes_keeps_no_suffix_extractor_of_its_own():
    from noroshi import outcomes
    assert not hasattr(outcomes, "_TLD") and not hasattr(outcomes, "_label") and not hasattr(outcomes, "_registrable")


def _one(tmp_path, domain, build, tokens=""):
    """A fresh store with one hospital domain, set up by build(store); returns its derived row."""
    store = synth.Store(tmp_path / "one.sqlite")
    build(store)
    frame = pd.DataFrame([{"sector": "hospital", "domain": domain, "start_url": f"https://{domain}/", "n_units": "1",
                           "beds_sum": "5", "name_tokens": tokens}])
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    out = derive_domains(store, frame, tmp_path / "none.jsonl", reg, tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc[domain]


def test_a_private_suffix_site_is_its_own_site(tmp_path):
    """pydicom.github.io redirecting to another github.io site is off its domain; its own platform link counts."""
    d = "pydicom.github.io"
    link = b'<a href="https://hackerone.com/pydicom">Sponsors</a>'

    def build(s):
        synth.rec(s, d, "homepage", b"<html>" + link + b"</html>", ctype="text/html", url=f"https://{d}/",
                  final="https://other.github.io/", sector="icsma")
        s.mark_done(d, 1, "measured")
    r = _one(tmp_path, d, build)
    assert r.redirected_offdomain is True
    assert r.platforms == "hackerone"


def test_in_flight_and_outage_rows_are_never_the_latest_record():
    from noroshi.outcomes import _latest
    from noroshi.store import FetchRecord

    def r(error, status=None, n=0):
        return FetchRecord("t.org", "hospital", "t.org", f"https://t.org/{n}", "sectxt_wellknown", 1, "identified",
                           "2026-11-20T00:00:00Z", status, None, [], None, None, 0, False, error, False)
    timeout = r("timeout", n=1)
    for label in ("in_flight", "outage", "cancelled"):
        assert _latest({1: [timeout, r(label, n=2)], 2: []}, "sectxt_wellknown") is timeout
        assert _latest({1: [r(label, n=2)], 2: []}, "sectxt_wellknown") is None


# ---- B6: a valid file seen in either pass stands

def test_valid_in_pass_1_and_404_in_pass_2_stays_valid_and_the_passes_disagree(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        _cms(s, synth.CMS_OK)
        s.mark_done("t.org", 1, "measured")
        _home(s, pass_no=2)
        _wk(s, b"<html>not found</html>", status=404, ctype="text/html", pass_no=2)
        _root(s, None, status=404, pass_no=2)
        _cms(s, None, status=404, ctype="text/html", pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "valid" and r.main is True and r.sectxt_location == "well-known"
    assert r.cmshpt_class == "present_valid"
    assert r.passes_disagree is True


def test_a_later_valid_file_is_used_over_an_earlier_one(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.EXPIRED)
        s.mark_done("t.org", 1, "measured")
        _home(s, pass_no=2)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"), pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _mini(tmp_path, build)
    assert r.sectxt_class == "valid" and r.strict is True and r.passes_disagree is True


def test_passes_agree_when_one_pass_measured_the_files(tmp_path):
    def build(s):
        _home(s)
        _wk(s, synth.GOOD.replace(b"{d}", b"t.org"))
        _cms(s, synth.CMS_OK)
        s.mark_done("t.org", 1, "measured")
    assert _mini(tmp_path, build).passes_disagree is False


# ---- B4: registry listings follow the final site (and B13: the final site's registrable domain)

def _redirected(tmp_path, reg, domain="alias.org", final="https://www.hub-parent.com/home", status="measured"):
    store = synth.Store(tmp_path / f"{domain}.sqlite")
    if status == "measured":
        synth.rec(store, domain, "homepage", synth.HOME, ctype="text/html", url=f"https://{domain}/", final=final)
        synth.rec(store, domain, "robots", b"", status=404, url="https://www.hub-parent.com/robots.txt")
        synth.rec(store, domain, "sectxt_wellknown", None, status=404, ctype="text/html",
                  url="https://www.hub-parent.com/.well-known/security.txt")
        synth.rec(store, domain, "sectxt_root", None, status=404, ctype="text/html",
                  url="https://www.hub-parent.com/security.txt")
    else:
        synth.rec(store, domain, "homepage", b"no", status=403, ctype="text/html", url=f"https://{domain}/",
                  final=final, blocked=True)
    store.mark_done(domain, 1, status)
    frame = pd.DataFrame([{"sector": "device", "domain": domain, "start_url": f"https://{domain}/", "n_units": "1",
                           "beds_sum": "", "name_tokens": ""}])
    out = derive_domains(store, frame, tmp_path / "none.jsonl", reg, tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc[domain]


def test_a_listing_of_the_final_site_counts_for_a_redirecting_domain(tmp_path):
    reg = synth.Registries(platforms={"bugcrowd": {"hub-parent.com"}}, diodb=set(), cna={"hub-parent.com"})
    r = _redirected(tmp_path, reg)
    assert r.final_domain == "hub-parent.com"
    assert r.cna is True and r.platforms == "bugcrowd" and r.listing is True and r.broad is True
    assert r.listing_matched_on == "final"


def test_listing_matched_on_listed_both_or_none(tmp_path):
    reg = synth.Registries(platforms={}, diodb={"alias.org"}, cna={"hub-parent.com"})
    r = _redirected(tmp_path, reg)
    assert r.diodb is True and r.cna is True and r.listing_matched_on == "both"
    reg = synth.Registries(platforms={}, diodb={"alias.org"}, cna=set())
    assert _redirected(tmp_path / "b", reg).listing_matched_on == "listed"
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    r = _redirected(tmp_path / "c", reg)
    assert r.listing_matched_on == "" and r.listing is False


def test_final_domain_is_empty_when_not_measured(tmp_path):
    reg = synth.Registries(platforms={}, diodb=set(), cna={"hub-parent.com"})
    r = _redirected(tmp_path, reg, status="blocked")
    assert r.final_domain == "" and r.cna is False and r.listing_matched_on == ""


# ---- B2: which signature the domain's own site saw

def _ua(tmp_path, build, domain="a.org"):
    store = synth.Store(tmp_path / "ua.sqlite")
    build(store)
    frame = pd.DataFrame([{"sector": "hospital", "domain": domain, "start_url": f"https://{domain}/", "n_units": "1",
                           "beds_sum": "5", "name_tokens": ""}])
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    out = derive_domains(store, frame, tmp_path / "none.jsonl", reg, tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc[domain, "ua_kind"]


def _r(store, url, purpose, domain="a.org", **kw):
    synth.rec(store, domain, purpose, kw.pop("body", b"x"), url=url, ctype=kw.pop("ctype", "text/html"), **kw)


def test_own_site_refusal_after_the_homepage_makes_the_domain_browser(tmp_path):
    def build(s):
        _r(s, "https://a.org/", "homepage", body=synth.HOME)
        _r(s, "https://a.org/robots.txt", "robots", status=404)
        _r(s, "https://a.org/.well-known/security.txt", "sectxt_wellknown", status=403, blocked=True)
        _r(s, "https://a.org/.well-known/security.txt", "sectxt_wellknown", status=404, ua="browser")
        s.mark_done("a.org", 1, "measured")
    assert _ua(tmp_path, build) == "browser"


def test_a_browser_hop_at_a_third_party_leaves_the_domain_identified(tmp_path):
    def build(s):
        _r(s, "https://tracker.test/x", "candidate", domain="other.org", status=403, blocked=True)  # tracker refused
        _r(s, "https://tracker.test/x", "candidate", domain="other.org", status=200, ua="browser")
        synth.rec(s, "a.org", "homepage", synth.HOME, ctype="text/html", url="https://a.org/",
                  final="https://www.a.org/", ua="browser")
        s.db.execute("UPDATE requests SET redirect_chain = ? WHERE domain = 'a.org'",
                     ('["https://a.org/", "https://tracker.test/hop"]',))
        s.db.commit()
        _r(s, "https://www.a.org/robots.txt", "robots", status=404)
        s.mark_done("a.org", 1, "measured")
    assert _ua(tmp_path, build) == "identified"


def test_a_refusal_with_no_browser_request_after_it_stays_identified(tmp_path):
    def build(s):
        _r(s, "https://a.org/", "homepage", status=403, blocked=True)  # the retry was never sent (no budget)
        s.mark_done("a.org", 1, "blocked")
    assert _ua(tmp_path, build) == "identified"


def test_reused_copies_of_a_browser_measurement_on_the_final_site_are_browser(tmp_path):
    def build(s):
        synth.rec(s, "a1.org", "homepage", b"no", status=403, ctype="text/html", url="https://a1.org/",
                  final="https://hub.org/", blocked=True)
        synth.rec(s, "a1.org", "homepage", synth.HOME, ctype="text/html", url="https://a1.org/",
                  final="https://hub.org/", ua="browser")
        synth.rec(s, "a1.org", "robots", b"", status=404, url="https://hub.org/robots.txt", ua="browser")
        s.mark_done("a1.org", 1, "measured")
        s.record(synth.FetchRecord("a.org", "hospital", "a.org", "https://a.org/", "homepage", 1, "identified",
                                   "2026-11-20T00:00:00Z", 200, "https://hub.org/", ["https://a.org/"], "text/html",
                                   None, 0, False, None, False, site="a.org"), synth.HOME)
        s.record(synth.FetchRecord("a.org", "hospital", "hub.org", "https://hub.org/robots.txt", "robots", 1,
                                   "browser", "2026-11-20T00:00:00Z", 404, "https://hub.org/robots.txt", [], "text/html",
                                   None, 0, False, None, False, site="hub.org", reused=True), b"")
        s.mark_done("a.org", 1, "measured")
    assert _ua(tmp_path, build) == "browser"


# ---- B9: first-seen follows the file's final URL; archived redirect targets are reported

def test_first_seen_input_and_lookup_use_the_chosen_files_final_url(tmp_path):
    good = synth.GOOD.replace(b"{d}", b"t.org")

    def build(s):
        _home(s)
        _wk(s, good, final="https://security.t-corp.com/files/security.txt")
        s.mark_done("t.org", 1, "measured")
    store = synth.Store(tmp_path / "m.sqlite")
    build(store)
    frame = pd.DataFrame([{"sector": "hospital", "domain": "t.org", "start_url": "https://t.org/", "n_units": "1",
                           "beds_sum": "5", "name_tokens": ""}])
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    wb = tmp_path / "w.jsonl"
    d = derive_domains(store, frame, tmp_path / "none.jsonl", reg, wb, synth.NOW)
    assert d.loc[0, "sectxt_url"] == "https://security.t-corp.com/files/security.txt"
    first, _ = wayback_inputs(d)
    assert first.to_dict("records") == [{"domain": "t.org", "host": "security.t-corp.com", "path": "/files/security.txt"}]
    wb.write_text(json.dumps({"domain": "t.org", "host": "t.org", "path": "/.well-known/security.txt",
                              "first_seen": "2001-01-01"}) + "\n")
    assert derive_domains(store, frame, tmp_path / "none.jsonl", reg, wb, synth.NOW).loc[0, "first_seen"] == ""
    wb.write_text(json.dumps({"domain": "t.org", "host": "security.t-corp.com", "path": "/files/security.txt",
                              "first_seen": "2018-03-04"}) + "\n")
    assert derive_domains(store, frame, tmp_path / "none.jsonl", reg, wb, synth.NOW).loc[0, "first_seen"] == "2018-03-04"
    store.close()


def test_archive_redirect_target_for_unreachable_hospital_domains(synthetic, tmp_path):
    store, frame, judg, reg, wb = synthetic
    redirects = tmp_path / "wayback_redirects.jsonl"
    redirects.write_text("".join(json.dumps(r) + "\n" for r in [
        {"domain": "down.org", "host": "down.org", "redirect_target": "newowner.com"},
        {"domain": "good.org", "host": "good.org", "redirect_target": "elsewhere.com"}]) + '{"domain": "cut')
    d = derive_domains(store, frame, judg, reg, wb, synth.NOW, redirects=redirects).set_index("domain")
    assert d.loc["down.org", "archive_redirect_target"] == "newowner.com"
    assert d.loc["good.org", "archive_redirect_target"] == ""  # only unreachable hospital domains
    assert d.loc["blocked.org", "archive_redirect_target"] == ""
    no_file = derive_domains(store, frame, judg, reg, wb, synth.NOW).set_index("domain")
    assert no_file.loc["down.org", "archive_redirect_target"] == ""


# ---- closing fixes, C1: the homepage comes only from a pass that measured the domain

def _passes(tmp_path, build, reg, sector="device"):
    """A fresh store with one domain `a.org`, set up by build(store); returns its derived row."""
    store = synth.Store(tmp_path / "p.sqlite")
    build(store)
    frame = pd.DataFrame([{"sector": sector, "domain": "a.org", "start_url": "https://a.org/", "n_units": "1",
                           "beds_sum": "", "name_tokens": "a"}])
    out = derive_domains(store, frame, tmp_path / "none.jsonl", reg, tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc["a.org"]


def _files_absent(s, host="https://www.a.org"):
    for purpose, path in (("sectxt_wellknown", "/.well-known/security.txt"), ("sectxt_root", "/security.txt")):
        synth.rec(s, "a.org", purpose, b"<html>nf</html>", status=404, ctype="text/html", url=f"{host}{path}")


def test_a_pass_that_ended_mismatch_gives_no_homepage_and_no_listing(tmp_path):
    def build(s):
        synth.rec(s, "a.org", "homepage", synth.HOME, ctype="text/html", url="https://a.org/",
                  final="https://www.a.org/")
        synth.rec(s, "a.org", "robots", b"busy", status=503, ctype="text/html", url="https://www.a.org/robots.txt")
        _files_absent(s)
        s.mark_done("a.org", 1, "measured")  # robots.txt answered 503, so pass 2 retried it
        synth.rec(s, "a.org", "homepage", synth.HOME, ctype="text/html", url="https://a.org/",
                  final="https://www.facebook.com/aorg", pass_no=2)
        s.mark_done("a.org", 2, "mismatch")
    reg = synth.Registries(platforms={"hackerone": {"facebook.com"}}, diodb=set(), cna={"facebook.com"})
    r = _passes(tmp_path, build, reg)
    assert (r.status, r.host, r.final_domain, r.redirected_offdomain) == ("measured", "www.a.org", "a.org", False)
    assert (r.platforms, r.cna, r.listing, r.listing_matched_on, r.registry_listing) == ("", False, False, "", False)
    assert (r.main, r.policy_page, r.policy_look, r.broad) == (False, "unmeasured", "robots_unreadable", None)


def test_a_pass_whose_homepage_answered_404_does_not_erase_the_measured_pass(tmp_path):
    home = synth.HOME.replace(b"</body>", b'<a href="https://hackerone.com/a_org">Report a vulnerability</a></body>')

    def build(s):
        synth.rec(s, "a.org", "homepage", home, ctype="text/html", url="https://a.org/")
        synth.rec(s, "a.org", "robots", b"User-agent: *\nDisallow: /admin\n", url="https://a.org/robots.txt")
        _files_absent(s, "https://a.org")
        synth.rec(s, "a.org", "cmshpt", b"busy", status=503, ctype="text/html", url="https://a.org/cms-hpt.txt")
        for path in PROBE_PATHS:
            synth.rec(s, "a.org", "probe", b"<html>nf</html>", status=404, ctype="text/html", url=f"https://a.org{path}")
        s.mark_done("a.org", 1, "measured")  # cms-hpt.txt answered 503, so pass 2 retried it
        synth.rec(s, "a.org", "homepage", b"<html>nf</html>", status=404, ctype="text/html", url="https://a.org/",
                  pass_no=2)
        s.mark_done("a.org", 2, "unreachable")
    r = _passes(tmp_path, build, synth.Registries(platforms={}, diodb=set(), cna=set()), sector="hospital")
    assert (r.status, r.platforms, r.listing, r.policy_page, r.broad) == ("measured", "hackerone", True, "no", True)


# ---- post-pilot Y2: what the judge sets aside, as derive reads it

from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from noroshi.judge import run_judging  # noqa: E402

PILOT = Path(__file__).parent / "fixtures" / "pilot"
LONG_HOME = (b"<html><title>A</title><body><p>" + b"Welcome to our hospital, open day and night for the valley. " * 5
             + b"</p></body></html>")


def _judged(tmp_path, build, verdict=(True, True)):
    """A fresh store with one device domain `a.org` (homepage, robots, absent files), set up further by build(store);
    judged with a fixed reply, then derived."""
    store = synth.Store(tmp_path / "y2.sqlite")
    synth.rec(store, "a.org", "homepage", LONG_HOME, ctype="text/html", url="https://a.org/", sector="device")
    synth.rec(store, "a.org", "robots", b"User-agent: *\nDisallow: /admin\n", url="https://a.org/robots.txt",
              sector="device")
    for purpose, path in (("sectxt_wellknown", "/.well-known/security.txt"), ("sectxt_root", "/security.txt")):
        synth.rec(store, "a.org", purpose, b"<html>nf</html>", status=404, ctype="text/html", url=f"https://a.org{path}",
                  sector="device")
    build(store)
    store.mark_done("a.org", 1, "measured")
    text = json.dumps({"invites_reports": verdict[0], "contact_method": verdict[1], "safe_harbor": False})
    judg = tmp_path / "y2.jsonl"
    run_judging(store, judg, ask=lambda *a, **k: SimpleNamespace(text=text))
    frame = pd.DataFrame([{"sector": "device", "domain": "a.org", "start_url": "https://a.org/", "n_units": "1",
                           "beds_sum": "", "name_tokens": ""}])
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())
    out = derive_domains(store, frame, judg, reg, tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc["a.org"]


def _probes(store, body_of, final_of=lambda p: None):
    for path in PROBE_PATHS:
        synth.rec(store, "a.org", "probe", body_of(path), ctype="text/html", url=f"https://a.org{path}",
                  final=final_of(path), sector="device")


def test_a_site_answering_every_path_with_one_page_has_a_complete_look_and_no_policy_page(tmp_path):
    shell = (PILOT / "grants_probe_shell.html").read_bytes()
    r = _judged(tmp_path, lambda s: _probes(s, lambda p: shell + f"<!-- {p} -->".encode()))
    assert (r.policy_page, r.policy_look, r.policy_pages_unjudged, r.broad) == ("no", "", 0, False)


def test_an_unreadable_page_makes_the_look_incomplete(tmp_path):
    shell = b'<html><head><title>Clinic Locator</title></head><body><app-root></app-root></body></html>'

    def build(s):  # birthcontrol.gov in the pilot: an empty script shell at every path
        _probes(s, lambda p: shell)
    r = _judged(tmp_path, build)
    assert (r.policy_page, r.policy_look, r.policy_pages_unjudged, r.broad) == ("unmeasured", "unreadable_page", 6, None)


def test_a_followed_meta_refresh_is_not_unreadable_and_its_target_stands_in(tmp_path):
    refresh = (PILOT / "va_policy_refresh.html").read_bytes()
    policy = (b"<h1>Vulnerability Disclosure Policy</h1><p>To report a security vulnerability email sec@a.org.</p>"
              + b"<p>We thank every researcher who writes to us in good faith. </p>" * 3)

    def build(s):
        for path in PROBE_PATHS:
            synth.rec(s, "a.org", "probe", refresh if path == "/security" else b"<html>nf</html>", ctype="text/html",
                      status=200 if path == "/security" else 404, url=f"https://a.org{path}", sector="device")
            if path == "/security":  # the crawler followed the refresh: its target, with the same purpose
                synth.rec(s, "a.org", "probe", policy, ctype="text/html", sector="device",
                          url="https://digital.va.gov/vulnerability-disclosure-policy/")
    r = _judged(tmp_path, build)
    assert (r.policy_page, r.policy_look, r.policy_pages_unjudged) == ("yes", "", 0)


# ---- post-pilot Y4: a file refused to both signatures with a plain denial is forbidden: no file

DENIED = (PILOT / "insulet_wellknown_403.txt").read_bytes()  # insulet.com's 13-byte "Access denied" (#753, #768)
CHALLENGE = b"<html><head><title>Just a moment...</title></head><body></body></html>"
WK, ROOT, CMS = "/.well-known/security.txt", "/security.txt", "/cms-hpt.txt"
PURPOSE = {WK: "sectxt_wellknown", ROOT: "sectxt_root", CMS: "cmshpt"}


def _refused_twice(s, path, body=DENIED, pass_no=1):
    for ua in ("identified", "browser"):
        synth.rec(s, "t.org", PURPOSE[path], body, status=403, ctype="text/plain", url=f"https://t.org{path}",
                  blocked=True, ua=ua, pass_no=pass_no)


def _file(s, path, status, body=None, pass_no=1, status_none=False, **kw):
    synth.rec(s, "t.org", PURPOSE[path], body, status=None if status_none else status, url=f"https://t.org{path}",
              pass_no=pass_no, **kw)


def _forbidden_case(tmp_path, build):
    def full(s):
        _home(s)
        build(s)
        s.mark_done("t.org", 1, "measured")
    return _mini(tmp_path, full)


def test_a_wellknown_file_refused_to_both_signatures_is_forbidden_and_counts_as_no_file(tmp_path):
    r = _forbidden_case(tmp_path, lambda s: (_refused_twice(s, WK), _file(s, ROOT, 404), _file(s, CMS, 404)))
    assert (r.sectxt_class, r.sectxt_location, r.main, r.strict, r.file_forbidden) == ("forbidden", "", False, False, True)
    assert r.cmshpt_class == "absent"


def test_forbidden_ranks_just_above_absent_and_unmeasured_still_wins(tmp_path):
    cases = [  # (the two locations, the domain's class, file_forbidden)
        (lambda s: (_refused_twice(s, WK), _refused_twice(s, ROOT)), "forbidden", True),
        (lambda s: (_file(s, WK, 404), _refused_twice(s, ROOT)), "forbidden", True),
        (lambda s: (_refused_twice(s, WK), _file(s, ROOT, None, error="timeout", status_none=True)), "unmeasured", True),
        (lambda s: (_refused_twice(s, WK), _file(s, ROOT, 200, b"<html>soft 404</html>", ctype="text/html")),
         "html_error", True),
        (lambda s: (_refused_twice(s, WK), _file(s, ROOT, 200, b"Contact: mailto:a@t.org\n")), "valid_with_problems",
         True),
        (lambda s: (_refused_twice(s, WK, body=CHALLENGE), _file(s, ROOT, 404)), "unmeasured", False),  # a block page
    ]
    for i, (build, cls, forbidden) in enumerate(cases):
        r = _forbidden_case(tmp_path / str(i), build)
        assert (r.sectxt_class, r.file_forbidden) == (cls, forbidden), i


def test_a_forbidden_cms_hpt_file_is_no_file(tmp_path):
    r = _forbidden_case(tmp_path, lambda s: (_file(s, WK, 404), _file(s, ROOT, 404), _refused_twice(s, CMS)))
    assert (r.cmshpt_class, r.sectxt_class, r.main, r.file_forbidden) == ("forbidden", "absent", False, True)


def test_a_forbidden_answer_stands_like_absent_against_a_later_failure(tmp_path):
    def build(s):
        _refused_twice(s, WK)
        _file(s, ROOT, 404)
        _home(s, pass_no=2)  # pass 2 (for another reason) timed out at the well-known file
        _file(s, WK, None, pass_no=2, error="timeout", status_none=True)
        _file(s, ROOT, 404, pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _forbidden_case(tmp_path, build)
    assert (r.sectxt_class, r.file_forbidden, r.passes_disagree) == ("forbidden", True, False)


def test_forbidden_is_decided_in_the_pass_its_record_came_from(tmp_path):
    def build(s):  # pass 1: refused twice; pass 2: the identified attempt refused and its retry never sent
        _refused_twice(s, WK)
        _file(s, ROOT, 404)
        _home(s, pass_no=2)
        synth.rec(s, "t.org", "sectxt_wellknown", DENIED, status=403, ctype="text/plain", url=f"https://t.org{WK}",
                  blocked=True, pass_no=2)
        _file(s, ROOT, 404, pass_no=2)
        s.mark_done("t.org", 2, "measured")
    r = _forbidden_case(tmp_path, build)
    assert (r.sectxt_class, r.file_forbidden) == ("forbidden", True)  # pass 2 holds no result; pass 1's stands


# ---- post-pilot Y4: two browser counts, after part X's per-path retry

def test_site_switched_counts_only_a_site_switched_to_the_browser_signature(tmp_path):
    reg = synth.Registries(platforms={}, diodb=set(), cna=set())

    def switched(s):  # the homepage refused the identified signature and the browser retry loaded: switched
        synth.rec(s, "a.org", "homepage", b"no", status=403, ctype="text/html", url="https://a.org/", blocked=True)
        synth.rec(s, "a.org", "homepage", LONG_HOME, ctype="text/html", url="https://a.org/", ua="browser")
        s.mark_browser("a.org", 1)
        s.mark_done("a.org", 1, "measured")

    def path_only(s):  # one path refused to both signatures (abbott.com in the pilot): retried once, not switched
        synth.rec(s, "a.org", "homepage", LONG_HOME, ctype="text/html", url="https://a.org/")
        for ua in ("identified", "browser"):
            synth.rec(s, "a.org", "sectxt_wellknown", DENIED, status=403, url="https://a.org/.well-known/security.txt",
                      blocked=True, ua=ua)
        s.mark_done("a.org", 1, "measured")

    def elsewhere(s):  # another site was switched in the pass: not this domain's
        synth.rec(s, "a.org", "homepage", LONG_HOME, ctype="text/html", url="https://a.org/")
        s.mark_browser("b.org", 1)
        s.mark_done("a.org", 1, "measured")
    got = [(r.ua_kind, r.site_switched) for r in (_passes(tmp_path / str(i), b, reg)
                                                  for i, b in enumerate((switched, path_only, elsewhere)))]
    assert got == [("browser", True), ("browser", False), ("identified", False)]


def test_site_switched_follows_the_homepage_to_its_final_site_in_pass_2(tmp_path):
    def build(s):
        synth.rec(s, "a.org", "homepage", LONG_HOME, ctype="text/html", url="https://a.org/")
        s.mark_done("a.org", 1, "measured")
        synth.rec(s, "a.org", "homepage", LONG_HOME, ctype="text/html", url="https://a.org/", pass_no=2,
                  final="https://www.a-parent.com/")
        s.mark_browser("a-parent.com", 2)
        s.mark_done("a.org", 2, "measured")
    r = _passes(tmp_path, build, synth.Registries(platforms={}, diodb=set(), cna=set()))
    assert r.site_switched is True


# ---- post-pilot Y4 (audit M8): a file read on another site is flagged

def test_a_file_whose_final_url_is_on_another_site_is_flagged(tmp_path):
    good = b"Contact: mailto:sec@t.org\nExpires: 2030-01-01T00:00:00Z\n"
    cms = b"location-name: A\nmrf-url: https://x.org/f.json\n"

    def build(s):  # bridgeinteract.io's security.txt and wchs-wy.org's cms-hpt.txt, as the pilot read them
        _home(s)
        synth.rec(s, "t.org", "sectxt_wellknown", good, url=f"https://t.org{WK}",
                  final="https://bridge-patient-portal.github.io/security/security.txt")
        synth.rec(s, "t.org", "cmshpt", cms, url=f"https://t.org{CMS}", final="https://t-org.usrfiles.com/ugd/cms-hpt.txt")
        s.mark_done("t.org", 1, "measured")
    r = _mini(tmp_path / "a", build)
    assert (r.sectxt_class, r.sectxt_offdomain, r.cmshpt_class, r.cmshpt_offdomain) == \
        ("valid", True, "present_valid", True)

    def same_site(s):
        _home(s)
        synth.rec(s, "t.org", "sectxt_wellknown", good, url=f"https://t.org{WK}", final="https://www.t.org/security.txt")
        synth.rec(s, "t.org", "cmshpt", cms, url=f"https://t.org{CMS}")
        s.mark_done("t.org", 1, "measured")
    r = _mini(tmp_path / "b", same_site)
    assert (r.sectxt_offdomain, r.cmshpt_offdomain) == (False, False)

    def no_files(s):
        _home(s)
        _file(s, WK, 404)
        _file(s, ROOT, 404)
        _refused_twice(s, CMS)
        s.mark_done("t.org", 1, "measured")
    r = _mini(tmp_path / "c", no_files)
    assert (r.sectxt_class, r.sectxt_offdomain, r.cmshpt_class, r.cmshpt_offdomain) == \
        ("absent", None, "forbidden", None)


# ---- post-pilot Y4 (audit I5 c): a homepage with almost no text gives no links

def _home_case(tmp_path, build):
    store = synth.Store(tmp_path / "h.sqlite")
    build(store)
    frame = pd.DataFrame([{"sector": "device", "domain": "a.org", "start_url": "https://a.org/", "n_units": "1",
                           "beds_sum": "", "name_tokens": ""}])
    out = derive_domains(store, frame, tmp_path / "none.jsonl", synth.Registries(platforms={}, diodb=set(), cna=set()),
                         tmp_path / "w.jsonl", synth.NOW)
    store.close()
    return out.set_index("domain").loc["a.org"]


def _measured_pass(s, home, pass_no=1, final=None):
    synth.rec(s, "a.org", "homepage", home, ctype="text/html", url="https://a.org/", pass_no=pass_no, final=final,
              sector="device")
    host = final.rstrip("/") if final else "https://a.org"
    synth.rec(s, "a.org", "robots", b"User-agent: *\nDisallow: /admin\n", url=f"{host}/robots.txt", pass_no=pass_no,
              sector="device")
    for purpose, path in (("sectxt_wellknown", WK), ("sectxt_root", ROOT)):
        synth.rec(s, "a.org", purpose, b"<html>nf</html>", status=404, ctype="text/html", url=f"{host}{path}",
                  pass_no=pass_no, sector="device")
    for path in PROBE_PATHS:
        synth.rec(s, "a.org", "probe", b"<html>nf</html>", status=404, ctype="text/html", url=f"{host}{path}",
                  pass_no=pass_no, sector="device")
    s.mark_done("a.org", pass_no, "measured")


ANGULAR_SHELL = b'<html><head><title>Clinic Locator | HHS</title></head><body><app-root></app-root></body></html>'


def test_a_homepage_with_fewer_than_200_visible_characters_makes_the_look_incomplete(tmp_path):
    r = _home_case(tmp_path / "a", lambda s: _measured_pass(s, ANGULAR_SHELL))
    assert (r.policy_page, r.policy_look, r.broad) == ("unmeasured", "homepage_unreadable", None)
    r = _home_case(tmp_path / "b", lambda s: _measured_pass(s, LONG_HOME))
    assert (r.policy_page, r.policy_look, r.broad) == ("no", "", False)


def test_a_homepage_is_unreadable_only_when_every_measured_pass_read_it_so(tmp_path):
    r = _home_case(tmp_path, lambda s: (_measured_pass(s, ANGULAR_SHELL), _measured_pass(s, LONG_HOME, pass_no=2)))
    assert (r.policy_page, r.policy_look) == ("no", "")


def test_the_homepage_a_meta_refresh_led_to_is_the_one_read(tmp_path):
    refresh = b'<html><head><meta http-equiv="refresh" content="0;url=https://www.a.org/"></head><body></body></html>'

    def build(s):
        synth.rec(s, "a.org", "homepage", refresh, ctype="text/html", url="https://a.org/", sector="device")
        _measured_pass(s, LONG_HOME)  # its homepage record is the target, newer than the refresh page
    r = _home_case(tmp_path, build)
    assert (r.policy_page, r.policy_look) == ("no", "")


# ---- post-pilot Y4: the homepage of a pass is store.homepage_record, also in derive

def test_derive_reads_the_homepage_that_answered_when_a_pass_holds_several(tmp_path):
    """The other host form after a network error and a followed meta refresh (part X): the newest homepage record among
    outcomes is the homepage; host, final_domain, links and requests_sent follow it."""
    refresh = b'<html><head><meta http-equiv="refresh" content="0;url=https://www.a-new.org/"></head><body></body></html>'
    target = LONG_HOME.replace(b"</body>", b'<a href="https://hackerone.com/a_new">Report a vulnerability</a></body>')

    def build(s):
        synth.rec(s, "a.org", "homepage", None, status=None, error="dns", url="https://a.org/", sector="device")
        synth.rec(s, "a.org", "homepage", refresh, ctype="text/html", url="https://www.a.org/", sector="device")
        synth.rec(s, "a.org", "homepage", target, ctype="text/html", url="https://www.a-new.org/", sector="device")
        synth.rec(s, "a.org", "homepage", None, status=None, error="outage", url="https://a.org/", sector="device")
        synth.rec(s, "a.org", "robots", b"User-agent: *\n", url="https://www.a-new.org/robots.txt", sector="device")
        s.mark_done("a.org", 1, "measured")
    r = _passes(tmp_path, build, synth.Registries(platforms={}, diodb=set(), cna=set()))
    assert (r.host, r.final_domain, r.redirected_offdomain, r.platforms) == ("www.a-new.org", "a-new.org", True,
                                                                             "hackerone")
    assert r.requests_sent == 5  # every homepage attempt was sent, the outage one too
