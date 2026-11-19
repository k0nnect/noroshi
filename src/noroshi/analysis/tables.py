"""Every table of the paper, and the flat dictionary of numbers behind them.

Each numeric cell is created through `Numbers.put`, so a number in a table CSV is always in the
dictionary under a dotted key `<sector>.<level>.<measure>.<stat>`. Shares and interval bounds are
stored unrounded. Domains that could not be measured never enter a primary denominator.
"""
import csv
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

from noroshi.analysis.stats import exact_ci, fisher_2x2, kappa_and_note, mcnemar_exact
from noroshi.look import REASONS

SECTORS = ("hospital", "federal", "device", "icsma", "ehr")
MEASURES = ("main", "strict", "policy", "listing", "broad")
_NOT_IN_FRAME = ("optout", "not_crawled")
_OK = ("valid", "valid_with_problems")
_PCT = ("share", "ci_lo", "ci_hi")  # shares and the bounds of their intervals: the only cells shown as percentages


class Numbers(dict):
    def put(self, key: str, value):
        if isinstance(value, (np.integer,)):
            value = int(value)
        elif isinstance(value, (np.floating, float)):
            value = None if math.isnan(value) else float(value)
        elif isinstance(value, np.bool_):
            value = bool(value)
        self[key] = value
        return value


def _b(v):
    """True, False, or None from a bool, a CSV string, or a missing value."""
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        return True if s == "true" else False if s == "false" else None
    return None


_BOOL_COLUMNS = ("main", "strict", "broad", "listing", "signed", "parsers_agree", "sectxt_text_plain", "lenient_contact",
                 "registry_listing", "file_forbidden", "site_switched", "passes_disagree", "redirected_offdomain")


def _norm_domains(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for c in _BOOL_COLUMNS:
        d[c] = pd.Series([_b(v) for v in (d[c] if c in d else [None] * len(d))], index=d.index, dtype=object)
    d["policy"] = pd.Series([{"yes": True, "no": False}.get(str(v)) for v in d["policy_page"]], index=d.index,
                            dtype=object)
    for c in ("n_units", "n_malformed_contacts"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    for c in ("sectxt_class", "cmshpt_class", "contact_types", "expires_status", "ua_kind", "mismatch_reason",
              "archive_redirect_target", "safe_harbor", "identity", "final_domain", "identity_page_sha256"):
        d[c] = (d[c] if c in d else pd.Series([""] * len(d), index=d.index)).fillna("").astype(str)
    return d


def _norm_hospitals(df: pd.DataFrame) -> pd.DataFrame:
    h = df.copy()
    for c in ("main", "strict", "broad", "listing", "registry_listing"):
        h[c] = pd.Series([_b(v) for v in h[c]], index=h.index, dtype=object)
    h["policy"] = pd.Series([{"yes": True, "no": False}.get(str(v)) for v in h["policy_page"]], index=h.index,
                            dtype=object)
    beds = pd.to_numeric(h["beds"], errors="coerce")
    h["beds_num"] = beds.where(beds > 0)
    h["n_units"] = pd.to_numeric(h["n_units"], errors="coerce")
    h["status"] = h["status"].fillna("").astype(str)
    return h


def _is_share(column: str, row: dict) -> bool:
    """A share or an interval bound on one: a share column, or a `value` cell whose `stat` names a share."""
    if column in _PCT:
        return True
    return column == "value" and str(row.get("stat", "")).endswith(tuple(f"_{c}" for c in _PCT))


def _cell(column: str, row: dict) -> str:
    """Markdown text of one cell: shares as percentages; counts, p-values and every other number plain."""
    v = row.get(column)
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if _is_share(column, row):
        return f"{100 * v:.1f}%"
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.4g}"


def _write(out: Path, name: str, rows: list[dict], columns: list[str], note: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if r.get(c) is None else repr(r[c]) if isinstance(r.get(c), float) else r.get(c)
                        for c in columns])

    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    lines += ["| " + " | ".join(_cell(c, r) for c in columns) + " |" for r in rows]
    if note:
        lines += ["", note]
    (out / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stats(num: Numbers, prefix: str, k: int, n: int, ci: bool = True) -> dict:
    """k of n with an exact interval; with n = 0 the share and interval are empty. `ci=False` is for units
    that are not independent (hospitals sharing a domain): no interval is computed or stored."""
    k, n = int(k), int(n)
    out = {"k": num.put(f"{prefix}.k", k), "n": num.put(f"{prefix}.n", n),
           "share": num.put(f"{prefix}.share", k / n if n else None)}
    if ci:
        lo, hi = exact_ci(k, n) if n else (None, None)
        out.update(ci_lo=num.put(f"{prefix}.ci_lo", lo), ci_hi=num.put(f"{prefix}.ci_hi", hi))
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


_NEEDS_NO_CRAWL = ("blocked", "unreachable")
SENSITIVITY_NOTE = ("Note: in this sensitivity table every unit in the frame counts, and an outcome that could not "
                    "be measured counts as absent, except that a registry listing (a bug bounty platform's scope, "
                    "disclose.io or a CNA entry) counts toward listing and broad even for a domain that was blocked "
                    "or unreachable, because a listing needs no crawl. Rows at the final_site level count one unit "
                    "per final site (the registrable domain a homepage landed on): listed domains that redirect to "
                    "one site count once, as positive when any of them is.")


def _counted(units: pd.DataFrame, measure: str, sensitivity: bool) -> pd.Series:
    """The values of one measure that enter a share: known ones; or (sensitivity) every unit's, empty counted as
    false, with a registry listing counted toward listing and broad for a blocked or unreachable domain."""
    values = units[measure]
    if not sensitivity:
        return values.dropna()
    out = values.map(lambda v: bool(v))
    if measure in ("listing", "broad"):
        out = out | (units.status.isin(_NEEDS_NO_CRAWL) & units.registry_listing.map(lambda v: v is True))
    return out


FLOW_NOTE = ("Note: requests_sent_purged, in the all-sectors row, counts requests that were sent to domains that later "
             "opted out and whose records were deleted (the sum of \"requests sent\" over the store's purge log); "
             "requests_sent counts only the requests whose records are still in the store. "
             "unreachable_archive_redirect, in the hospital row only, counts unreachable hospital domains whose last "
             "archived homepage capture redirected to another registrable domain.")
MISMATCH_REASONS = ("crawl_rule", "identity_other", "identity_unclear")


def _flow(d: pd.DataFrame, num: Numbers, out: Path, requests_sent_purged: int) -> None:
    """Table 1. A domain listed in several sector tables is counted in each sector's row and once in the all-sectors
    row (it was crawled once and has the same status everywhere); `multi_sector` counts those domains."""
    look_cols = [f"look_{r}" for r in REASONS]
    cols = ["sector", "frame", "multi_sector", "optout", "not_crawled", "blocked", "blocked_rate_limited",
            "unreachable", "unreachable_archive_redirect", "mismatch", *(f"mismatch_{r}" for r in MISMATCH_REASONS),
            "measured", "redirected_offdomain", "passes_disagree", "browser", "site_switched", "file_forbidden",
            "policy_unjudged", *look_cols, "probes_cut", "requests_sent", "requests_sent_purged"]
    sectors_of = d.groupby("domain").sector.nunique()
    several = set(sectors_of[sectors_of > 1].index)
    rows = []
    for sector in (*SECTORS, "all"):
        s = d.drop_duplicates("domain") if sector == "all" else d[d.sector == sector]
        vals = {"sector": sector, "frame": len(s), "multi_sector": int(s.domain.isin(several).sum())}
        for st in ("optout", "not_crawled", "blocked", "unreachable", "mismatch", "measured"):
            vals[st] = int((s.status == st).sum())
        measured = s[s.status == "measured"]
        vals["blocked_rate_limited"] = int(((s.status == "blocked") & (s.blocked_reason == "rate_limited")).sum())
        for reason in MISMATCH_REASONS:  # why a domain is a mismatch: a crawl rule, or the identity check's answer
            vals[f"mismatch_{reason}"] = int(((s.status == "mismatch") & (s.mismatch_reason == reason)).sum())
        vals["unreachable_archive_redirect"] = (  # the hospital row only: only hospitals are looked up
            int(((s.status == "unreachable") & (s.archive_redirect_target != "")).sum()) if sector == "hospital"
            else None)
        vals["redirected_offdomain"] = int((measured.redirected_offdomain == True).sum())  # noqa: E712
        vals["passes_disagree"] = int((measured.passes_disagree == True).sum())  # noqa: E712
        vals["browser"] = int((measured.ua_kind == "browser").sum())
        vals["site_switched"] = int((measured.site_switched == True).sum())  # noqa: E712
        vals["file_forbidden"] = int((measured.file_forbidden == True).sum())  # noqa: E712
        vals["policy_unjudged"] = int((measured.policy_page == "unmeasured").sum())
        given = measured.policy_look.fillna("").astype(str).str.split(";")
        for reason, col in zip(REASONS, look_cols):  # measured domains whose look was incomplete for this reason
            vals[col] = int(given.map(lambda rs, r=reason: r in rs).sum())
        vals["probes_cut"] = int((pd.to_numeric(measured.probes_cut, errors="coerce").fillna(0) > 0).sum())
        vals["requests_sent"] = int(pd.to_numeric(s.requests_sent, errors="coerce").fillna(0).sum())
        vals["requests_sent_purged"] = int(requests_sent_purged) if sector == "all" else None  # one total only
        for k in cols[1:]:
            if vals[k] is not None:
                num.put(f"flow.{sector}.{k}", vals[k])
        rows.append(vals)
    _write(out, "table1_flow", rows, cols, note=FLOW_NOTE)


def _hospital_measure_rows(h: pd.DataFrame, num: Numbers, tag: str, sensitivity: bool) -> list[dict]:
    rows = []
    in_frame = h[~h.status.isin(_NOT_IN_FRAME) & (h.status != "")]
    for m in MEASURES:
        v = _counted(in_frame, m, sensitivity)
        rows.append({"sector": "hospital", "level": "hospital", "measure": m,
                     **_stats(num, f"hospital.{tag}hospital.{m}", sum(map(bool, v)), len(v), ci=False)})
        sub = in_frame.loc[v.index]
        known = sub[sub.beds_num.notna()]
        w = v.loc[known.index].map(bool)
        positive = float(known.beds_num[w].sum())
        total = float(known.beds_num.sum())
        p = f"hospital.{tag}beds.{m}"
        rows.append({"sector": "hospital", "level": "beds", "measure": m, "k": num.put(f"{p}.k", positive),
                     "n": num.put(f"{p}.n", total),
                     "share": num.put(f"{p}.share", positive / total if total else None),
                     "ci_lo": None, "ci_hi": None,
                     "n_hospitals": num.put(f"{p}.hospitals", len(known)),
                     "n_unknown_beds": num.put(f"{p}.excluded_unknown_beds", len(sub) - len(known))})
    return rows


_SHARE_COLS = ["sector", "level", "measure", "k", "n", "share", "ci_lo", "ci_hi", "n_hospitals", "n_unknown_beds"]


def _final_site_rows(s: pd.DataFrame, sector: str, num: Numbers) -> list[dict]:
    """Sensitivity rows with one unit per final site: listed domains whose homepages land on one registrable
    domain count once (positive when any of them is); a domain with no final site is its own unit."""
    unit = [f or f"listed:{dom}" for f, dom in zip(s.final_domain.fillna("").astype(str), s.domain)]
    rows = []
    for m in MEASURES:
        v = _counted(s, m, True).groupby(pd.Series(unit, index=s.index)).max()
        rows.append({"sector": sector, "level": "final_site", "measure": m,
                     **_stats(num, f"{sector}.sens_final_site.{m}", int(v.sum()), len(v))})
    return rows


def _shares(d: pd.DataFrame, h: pd.DataFrame, num: Numbers, out: Path, sensitivity: bool) -> None:
    tag = "sens_" if sensitivity else ""
    rows = []
    in_frame = d[~d.status.isin(_NOT_IN_FRAME)]
    for sector in SECTORS:
        s = in_frame[in_frame.sector == sector]
        for m in MEASURES:
            v = _counted(s, m, sensitivity)
            rows.append({"sector": sector, "level": "domain", "measure": m,
                         **_stats(num, f"{sector}.{tag}domain.{m}", sum(map(bool, v)), len(v))})
        if sensitivity:
            rows += _final_site_rows(s, sector, num)
        if sector == "hospital":
            rows += _hospital_measure_rows(h, num, tag, sensitivity)
    _write(out, "tableS1_sensitivity" if sensitivity else "table2_shares", rows, _SHARE_COLS,
           note=SENSITIVITY_NOTE if sensitivity else None)


_TYPES = [("GENERAL ACUTE CARE", "general_acute_care"), ("CRITICAL ACCESS", "critical_access"),
          ("PSYCHIATRIC", "psychiatric"), ("CHILDREN", "children"), ("LONG TERM CARE", "long_term_care"),
          ("REHABILITATION", "rehabilitation"), ("other", "other")]
_OWNERS = [("NON-PROFIT", "non_profit"), ("PROPRIETARY", "proprietary"),
           ("government, non-federal", "government"), ("unknown", "unknown")]
_BEDS = [("under 100", "under_100"), ("100 to 299", "100_299"), ("300 or more", "300_plus"), ("unknown", "unknown")]
_RURAL = [("yes", "yes"), ("no", "no"), ("unknown", "unknown")]
_SIZE = [("1", "1"), ("2 to 5", "2_5"), ("6 to 20", "6_20"), ("more than 20", "gt_20")]


def _type_group(t) -> str:
    t = str(t).strip().upper()
    return t if t in {x for x, _ in _TYPES} else "other"


def _owner_group(o) -> str:
    o = str(o).strip().upper()
    if o in ("NON-PROFIT", "PROPRIETARY"):
        return o
    return "government, non-federal" if o.startswith("GOVERNMENT - ") else "unknown"


def _beds_group(b) -> str:
    return "unknown" if pd.isna(b) else "under 100" if b < 100 else "100 to 299" if b < 300 else "300 or more"


def _rural_group(r) -> str:
    return {"true": "yes", "false": "no"}.get(str(r).strip().lower(), "unknown")


def _size_group(n) -> str:
    return "1" if n <= 1 else "2 to 5" if n <= 5 else "6 to 20" if n <= 20 else "more than 20"


def _count_rows(num: Numbers, rows: list, part: str, unit: str, variable: str, groups, labels: pd.Series,
                sel: pd.DataFrame, prefix: str, ci: bool) -> None:
    for label, slug in groups:
        sub = sel[labels.loc[sel.index] == label]
        st = _stats(num, f"{prefix}.{variable}.{slug}", sum(map(bool, sub.broad)), len(sub), ci=ci)
        rows.append({"part": part, "unit": unit, "variable": variable, "category": label,
                     "units_measured": st["n"], "units_broad": st["k"], "share": st["share"],
                     "ci_lo": st.get("ci_lo"), "ci_hi": st.get("ci_hi")})


def _characteristics(d: pd.DataFrame, h: pd.DataFrame, num: Numbers, out: Path) -> None:
    """Table 3, always descriptive (no regression model, whatever the number of positive domains)."""
    hd = d[(d.sector == "hospital") & (d.status == "measured")]
    cols = ["part", "unit", "variable", "category", "units_measured", "units_broad", "share", "ci_lo", "ci_hi",
            "p_value"]
    rows = []
    # (a) system size over all hospital domains, with the Fisher test of single against multi-hospital domains
    dom = hd[hd.broad.notna()]
    size = dom.n_units.map(lambda n: _size_group(n) if pd.notna(n) else "1")
    _count_rows(num, rows, "a", "hospital domain", "system_size", _SIZE, size, dom, "hospital.char_domain", True)
    single = dom[size == "1"]
    multi = dom[size != "1"]
    p = None
    if len(single) and len(multi):
        s_k, m_k = sum(map(bool, single.broad)), sum(map(bool, multi.broad))
        p = fisher_2x2(s_k, len(single) - s_k, m_k, len(multi) - m_k)
    rows.append({"part": "a", "unit": "hospital domain", "variable": "single_vs_multi",
                 "category": "single-hospital vs multi-hospital domains (Fisher exact)",
                 "p_value": num.put("hospital.char_domain.single_vs_multi.p", p)})
    # (b) single-hospital domains only: each domain is one hospital, so the units are independent
    known = h[h.broad.notna()]
    one = known[known.n_units.fillna(1) <= 1]
    # (c) every hospital, inheriting its domain's outcome: counts only, no interval and no test
    for part, unit, sel, prefix, ci in (("b", "single-hospital domain", one, "hospital.char_single", True),
                                       ("c", "hospital", known, "hospital.char_hospital", False)):
        for var, groups, g in (("type", _TYPES, sel.type.map(_type_group)),
                               ("owner", _OWNERS, sel.owner.map(_owner_group)),
                               ("beds", _BEDS, sel.beds_num.map(_beds_group)),
                               ("rural", _RURAL, sel.rural.map(_rural_group))):
            _count_rows(num, rows, part, unit, var, groups, g, sel, prefix, ci)
    _write(out, "table3_hospital_characteristics", rows, cols)


def _quality(d: pd.DataFrame, num: Numbers, out: Path) -> None:
    rows = []
    for sector in SECTORS:
        s = d[(d.sector == sector) & (d.main == True)]  # noqa: E712
        n = len(s)
        types = s.contact_types.str.split(";")
        counts = {f"contact_{t}": sum(t in x for x in types) for t in ("mailto", "https", "tel")}
        counts.update({f"expires_{e}": int((s.expires_status == e).sum())
                       for e in ("valid", "expired", "invalid", "missing", "multiple")})
        counts["signed"] = int((s.signed == True).sum())  # noqa: E712
        counts["text_plain"] = int((s.sectxt_text_plain == True).sum())  # noqa: E712
        counts["parsers_agree"] = int((s.parsers_agree == True).sum())  # noqa: E712
        for m, k in counts.items():
            rows.append({"sector": sector, "measure": m, **_stats(num, f"{sector}.quality.{m}", k, n)})
        inv = d[(d.sector == sector) & (d.sectxt_class == "invalid")]
        only = inv[(inv.contact_types == "") & ((inv.lenient_contact == True) | (inv.n_malformed_contacts > 0))]  # noqa: E712
        rows.append({"sector": sector, "measure": "only_lenient_or_malformed_contacts",
                     **_stats(num, f"{sector}.quality.only_lenient_or_malformed", len(only), len(inv))})
        found = d[(d.sector == sector) & (d.policy_page == "yes")]  # among domains with a policy page
        rows.append({"sector": sector, "measure": "policy_safe_harbor",
                     **_stats(num, f"{sector}.quality.policy_safe_harbor", int((found.safe_harbor == "yes").sum()),
                              len(found))})
    _write(out, "table4_quality", rows, ["sector", "measure", "k", "n", "share", "ci_lo", "ci_hi"],
           note=QUALITY_NOTE)


QUALITY_NOTE = ("Note: every measure counts among domains with the main outcome, except "
                "only_lenient_or_malformed_contacts (among domains whose security.txt is invalid) and "
                "policy_safe_harbor (among domains with a policy page: the share whose policy promises not to pursue "
                "good-faith reporters).")


FEDERAL_NOTE = ("Note: the federal control check: the broad outcome among measured federal domains whose broad value is "
                "known, counted per listed domain and per distinct final site (the registrable domain a homepage landed "
                "on; listed domains that lead to one site count once, as positive when any of them is).")


def _federal_control(d: pd.DataFrame, num: Numbers, out: Path) -> None:
    """Audit M9: several federal domains redirect to one agency site, so the control is given both ways."""
    s = d[(d.sector == "federal") & (d.status == "measured") & d.broad.notna()]
    rows = [{"unit": "listed domain", **_stats(num, "federal.control.listed_domain.broad", sum(map(bool, s.broad)),
                                               len(s))}]
    unit = pd.Series([f or f"listed:{dom}" for f, dom in zip(s.final_domain, s.domain)], index=s.index, dtype=object)
    by_site = s.broad.map(bool).groupby(unit).max()
    rows.append({"unit": "final site", **_stats(num, "federal.control.final_site.broad", int(by_site.sum()),
                                                len(by_site))})
    _write(out, "tableS3_federal_control", rows, ["unit", "k", "n", "share", "ci_lo", "ci_hi"], note=FEDERAL_NOTE)


AGREEMENT_NOTE = ("Note: judge agreement is over the distinct page texts both judges answered (pages screened out or set "
                  "aside by rule are not judged), identity agreement over the domains both raters answered (the "
                  "decision derive used, for the homepage each domain was measured from), both before any tie-break, "
                  "leaving out opted-out domains. share is the share of units on which the two agreed; kappa is "
                  "Cohen's kappa.")


def _agreement(d: pd.DataFrame, judgements: list[dict], identity: list[dict], num: Numbers, out: Path) -> None:
    """Table S2 (protocol 6.6): how often the two judges agreed on each question, and the two identity raters on the
    site, with Cohen's kappa; before the tie-break."""
    kept = set(d[d.status != "optout"].domain)
    pages: dict[str, dict] = {}
    for row in judgements:
        if row.get("domain") in kept and isinstance(row.get("a"), dict) and isinstance(row.get("b"), dict):
            pages.setdefault(row.get("text_sha256") or row.get("body_sha256") or str(len(pages)), row)
    from noroshi.identity import index_decisions

    by_view = index_decisions(identity)  # the decision derive matched for each domain's measured homepage
    used = {(name, key) for name, key, site in zip(d.domain, d.identity_page_sha256, d.identity)
            if name in kept and key and site in ("own", "other", "unclear")}
    latest = {name: by_view[(name, key)] for name, key in used
              if (name, key) in by_view and isinstance(by_view[(name, key)].get("a"), dict)
              and isinstance(by_view[(name, key)].get("b"), dict)}
    units = [("judge", q, [(p["a"].get(q), p["b"].get(q)) for p in pages.values()])
             for q in ("invites_reports", "contact_method", "safe_harbor")]
    units.append(("identity", "site", [(r["a"].get("site"), r["b"].get("site")) for r in latest.values()]))
    rows = []
    for check, question, pairs in units:
        a, b = [x for x, _ in pairs], [y for _, y in pairs]
        agree = sum(x == y for x, y in pairs)
        prefix = f"agreement.{check}.{question}"
        kappa, note = kappa_and_note(a, b)
        if check == "identity":  # rater answers recorded unclear because the rater twice gave no readable reply
            unread = num.put(f"{prefix}.unreadable_replies", sum(
                bool(isinstance(x, dict) and x.get("reply_unreadable"))
                for r in latest.values() for x in (r.get("a"), r.get("b"), r.get("tiebreak"))))
            if unread:
                said = ("1 rater answer was recorded as unclear because its reply could not be read twice"
                        if unread == 1 else f"{unread} rater answers were recorded as unclear because their replies "
                        "could not be read twice")
                note = "; ".join(x for x in (note, said) if x)
        rows.append({"check": check, "question": question, "n": num.put(f"{prefix}.n", len(pairs)),
                     "agree": num.put(f"{prefix}.agree", agree),
                     "share": num.put(f"{prefix}.share", agree / len(pairs) if pairs else None),
                     "kappa": num.put(f"{prefix}.kappa", kappa), "note": note})
    _write(out, "tableS2_agreement", rows, ["check", "question", "n", "agree", "share", "kappa", "note"],
           note=AGREEMENT_NOTE)


PARSER_COLUMNS = ["domain", "sectors", "sectxt_url", "sectxt_class", "contact_types", "ours_valid_contact",
                  "library_valid_contact"]


def write_parser_disagreements(domains: pd.DataFrame, path: Path) -> int:
    """Protocol 6.4 (T6): every measured domain whose two parsers disagree on whether the chosen security.txt has a
    valid contact, once each, with the file's URL and both verdicts (ours: a valid contact type was found). Returns how
    many."""
    d = _norm_domains(domains)
    off = d[(d.status == "measured") & (d.parsers_agree == False)]  # noqa: E712
    rows = []
    for name, g in off.groupby("domain", sort=True):
        first = g.iloc[0]
        ours = first.contact_types != ""
        rows.append({"domain": name, "sectors": ";".join(sorted(set(g.sector))), "sectxt_url": first.sectxt_url,
                     "sectxt_class": first.sectxt_class, "contact_types": first.contact_types,
                     "ours_valid_contact": ours, "library_valid_contact": not ours})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=PARSER_COLUMNS).to_csv(path, index=False)
    return len(rows)


def _paired(d: pd.DataFrame, num: Numbers, out: Path) -> None:
    s = d[(d.sector == "hospital") & (d.status == "measured") & (d.sectxt_class != "unmeasured")
          & (d.cmshpt_class != "unmeasured") & (d.cmshpt_class != "") & d.main.notna()]
    main = s.main.map(bool)
    cms = s.cmshpt_class == "present_valid"
    cell = {"main_and_cms_valid": int((main & cms).sum()), "main_only": int((main & ~cms).sum()),
            "cms_valid_only": int((~main & cms).sum()), "neither": int((~main & ~cms).sum())}
    rows = [{"stat": "domains", "value": num.put("paired.n", len(s))}]
    rows += [{"stat": k, "value": num.put(f"paired.{k}", v)} for k, v in cell.items()]
    b, c = cell["main_only"], cell["cms_valid_only"]
    rows.append({"stat": "mcnemar_p", "value": num.put("paired.mcnemar_p", mcnemar_exact(b, c) if b + c else None)})
    any_cms = s.cmshpt_class.isin(["present_valid", "present_invalid"])
    st = _stats(num, "paired.any_cms", int(any_cms.sum()), len(s))
    rows += [{"stat": f"any_cms_{k}", "value": v} for k, v in st.items()]
    _write(out, "table5_paired", rows, ["stat", "value"])


def build_all(domains: pd.DataFrame, hospitals: pd.DataFrame, out: Path, requests_sent_purged: int = 0,
              judgements: list[dict] = (), identity: list[dict] = ()) -> dict[str, float | int | str]:
    """Write every table into `out` and return the flat dictionary of numbers. `requests_sent_purged` is the sum of
    "requests sent" over the store's purge log: requests whose records a purge deleted. `judgements` and `identity`
    are the rows of the judge's and the identity check's result files, in the order written."""
    out = Path(out)
    d, h = _norm_domains(domains), _norm_hospitals(hospitals)
    num = Numbers()
    _flow(d, num, out, requests_sent_purged)
    _shares(d, h, num, out, sensitivity=False)
    _characteristics(d, h, num, out)
    _quality(d, num, out)
    _paired(d, num, out)
    _shares(d, h, num, out, sensitivity=True)
    _agreement(d, list(judgements), list(identity), num, out)
    _federal_control(d, num, out)
    return dict(num)


def write_numbers(numbers: dict, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(numbers, indent=2, sort_keys=True) + "\n", encoding="utf-8")
