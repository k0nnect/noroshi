# Independent recomputation of the flow and share tables (protocol section 9)

- Recomputation run: 2026-11-19 09:21:12 UTC (`run_at_utc` in the output; `date -u` read 09:21:41 UTC right after
  the run). Comparison with the study's tables made at 09:22 UTC, after the output was written and hashed.
- Command (from the repository root `the repository`):
  `uv run --frozen --offline python "../noroshi-paper/analysis/independent/recompute.py"`
- Code: `analysis/independent/recompute.py`
  (SHA-256 `17e462e5ce8754170958f5447312ed37ec102d4554da52951ec1bd3d77e8406c`)
- Output: `analysis/independent/recompute_output.json`
  (SHA-256 `538bd117c84dabb27202d749df58d9483bd3ddc4d94007cadb0cca65d3f4ae22`; the file was hashed before the study's
  tables were opened, and neither the code nor the output changed after that).

## Result in one line

116 cells compared, 116 match and 0 differ. All counts are equal, and the shares and Clopper-Pearson bounds are equal to
the last floating-point digit.

## What was read and what was not

Read: `protocol/PROTOCOL.md`; `src/noroshi/store.py`, `src/noroshi/config.py` and the docstring at the top of
`src/noroshi/fetch.py`; the five frame tables `data/frames/*_domains.csv`; `data/raw/crawl.sqlite` (tables `requests`
and `done`, opened read-only); bodies in `data/raw/blobs/`; `data/raw/identity.jsonl`; `info/optout.txt` (empty). I
also read part of the installed `sectxt` 0.9.8 package source to learn its API for the second-parser cross-check.
`data/raw/judgements.jsonl` was not needed for the main or strict outcome.

Not opened: `src/noroshi/outcomes.py`, `look.py`, `analysis/`, `cli.py`, `data/derived/`, the tests, and also
`sectxt_parse.py`, `blocking.py`, `domains.py` and `identity.py`. The code imports no `noroshi` module. No request went
to any website, no `noroshi` command was run, and nothing under `src/`, `protocol/`, `info/`, `data/` or `results/` was
changed. One early exploration command also tried to print the run logs `data/raw/identity_pass2.out`,
`judge_pass2.out`, `derive_pass2.out` and `analyze_pass2.out`. The permission system refused that command before it
ran, so none of those logs was read. I opened `results/tables/table1_flow.md` and `table2_shares.md` (and their
`.csv` copies, for exact values) only after the output above was written.

## Method (as I read the protocol)

1. **Status of each pass** (sections 5, 6.1 and 6.3), recomputed from the request rows. The homepage is the last
   homepage record of the pass that is an outcome. The status is decided in this order:
   - **mismatch**: a homepage request ended on `ww<digits>.<listed domain>` (any status, and checked before refusal and
     failure).
   - **blocked**: a 429 at one of the domain's own sites (the listed domain's site, the start URL's site, any homepage
     URL's site, or the homepage's final site) before the domain's last record of the pass, or a homepage hop stopped
     because its site had already answered 429.
   - **blocked**: the homepage was refused.
   - **unreachable**: the homepage failed, or ended with a status other than 200.
   - **mismatch**: the title or visible text holds a parked or for-sale marker.
   - **measured**: otherwise.

   I did not reimplement the third-party-platform list in `src/noroshi/domains.py` (see the unclear cases below).
2. **Combining the passes** (section 6.2): opted out, else measured if either pass measured the domain, else the
   status of the latest pass.
3. **Identity check** (section 6.3) on the homepage of the latest measured pass. Domains listed only in the federal
   table are exempt. The decision is recomputed from the raters (two raters agree, or else the tie-break model) and
   matched to the homepage view:
   - a decision from the same pass with the same final URL is used directly;
   - otherwise a decision from another pass is used only if that pass's homepage shows the same view, meaning identical
     bytes and URL, or an equal view as I extract it (host; title, `og:site_name` and description cut to 300
     characters; the first 2,000 characters of visible text and the last 800 after them).

   A domain decided `other` or `unclear` becomes a mismatch.
4. **Each `security.txt` location in each pass** (section 6.4):
   - **forbidden**: an identified 401/403 and its browser retry at the same address are both 401/403, both are plain
     (no challenge marker and no `<script`), and the homepage of that pass answered 200 and was not refused;
   - **absent**: a measured 4xx;
   - **HTML error page**: a 200 that is HTML (content type contains `html`, or the body starts with an
     html/head/body/doctype tag);
   - **invalid**: a 200 text with no valid contact, or a final address that is not https;
   - **valid with problems**: a valid contact but no single valid, unexpired `Expires`, judged against the record's
     `started_at`;
   - **valid**: otherwise.

   A refused or failed attempt, or any other status, is unmeasured, and a location with no record was never requested.
   PGP-signed files are unwrapped before parsing.
5. **Across passes** (section 6.2): the most recent result (a class or forbidden) is used. A failed attempt never
   replaces a result, and a valid or valid-with-problems result in either pass stands. Records count from every pass
   that ran, blocked passes included.
6. **The domain's class**: valid anywhere, then valid with problems anywhere, then unmeasured if either location is
   unmeasured or was never requested, then invalid, HTML error page, forbidden and absent.
7. **Main outcome**: k counts measured domains of class valid or valid with problems; n counts measured domains whose
   class is not unmeasured. **Strict outcome**: k counts class valid, over the same n. Exact intervals come from
   `scipy.stats.beta` quantiles (Clopper-Pearson, 95%).

## Cell-by-cell comparison

### Flow (table1_flow): mine / study

| sector | listed | measured | unreachable | blocked | of which rate-limited | mismatch | crawl rule | identity other | identity unclear | opted out | not crawled |
|---|---|---|---|---|---|---|---|---|---|---|---|
| hospital | 2652 / 2652 | 1721 / 1721 | 615 / 615 | 151 / 151 | 7 / 7 | 165 / 165 | 5 / 5 | 96 / 96 | 64 / 64 | 0 / 0 | 0 / 0 |
| federal | 124 / 124 | 101 / 101 | 20 / 20 | 3 / 3 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| device | 525 / 525 | 436 / 436 | 49 / 49 | 14 / 14 | 2 / 2 | 26 / 26 | 3 / 3 | 7 / 7 | 16 / 16 | 0 / 0 | 0 / 0 |
| icsma | 75 / 75 | 67 / 67 | 6 / 6 | 1 / 1 | 1 / 1 | 1 / 1 | 0 / 0 | 1 / 1 | 0 / 0 | 0 / 0 | 0 / 0 |
| ehr | 200 / 200 | 181 / 181 | 15 / 15 | 1 / 1 | 0 / 0 | 3 / 3 | 0 / 0 | 1 / 1 | 2 / 2 | 0 / 0 | 0 / 0 |
| all distinct | 3557 / 3557 | 2487 / 2487 | 705 / 705 | 170 / 170 | 10 / 10 | 195 / 195 | 8 / 8 | 105 / 105 | 82 / 82 | 0 / 0 | 0 / 0 |

That is 66 cells, and all 66 match.

### Shares (table2_shares, per listed domain): mine / study

| sector | measure | k | n | share | 95% CI (exact) |
|---|---|---|---|---|---|
| hospital | main | 132 / 132 | 1682 / 1682 | 7.8% / 7.8% | 6.6–9.2% / 6.6–9.2% |
| hospital | strict | 32 / 32 | 1682 / 1682 | 1.9% / 1.9% | 1.3–2.7% / 1.3–2.7% |
| federal | main | 4 / 4 | 99 / 99 | 4.0% / 4.0% | 1.1–10.0% / 1.1–10.0% |
| federal | strict | 0 / 0 | 99 / 99 | 0.0% / 0.0% | 0.0–3.7% / 0.0–3.7% |
| device | main | 30 / 30 | 430 / 430 | 7.0% / 7.0% | 4.8–9.8% / 4.8–9.8% |
| device | strict | 22 / 22 | 430 / 430 | 5.1% / 5.1% | 3.2–7.6% / 3.2–7.6% |
| icsma | main | 7 / 7 | 61 / 61 | 11.5% / 11.5% | 4.7–22.2% / 4.7–22.2% |
| icsma | strict | 6 / 6 | 61 / 61 | 9.8% / 9.8% | 3.7–20.2% / 3.7–20.2% |
| ehr | main | 8 / 8 | 179 / 179 | 4.5% / 4.5% | 1.9–8.6% / 1.9–8.6% |
| ehr | strict | 6 / 6 | 179 / 179 | 3.4% / 3.4% | 1.2–7.2% / 1.2–7.2% |

That is 50 cells (k, n, share and both bounds), and all 50 match. The comparison read the `.csv` values: the
differences are 0 for every share and every bound, not just after rounding.

The study's share table has no all-sectors row. Mine, with no study cell to compare against, is: main 178/2434 = 7.3%
(6.3–8.4%) and strict 64/2434 = 2.6% (2.0–3.3%).

My `security.txt` classes among measured domains were:
- hospital: valid 32, valid with problems 100, invalid 3, HTML 76, forbidden 58, absent 1413, unmeasured 39.
- federal: valid with problems 4, HTML 13, forbidden 1, absent 81, unmeasured 2.
- device: valid 22, valid with problems 8, invalid 2, HTML 25, forbidden 9, absent 364, unmeasured 6.
- icsma: valid 6, valid with problems 1, invalid 1, HTML 6, forbidden 6, absent 41, unmeasured 6.
- ehr: valid 6, valid with problems 2, HTML 16, forbidden 2, absent 153, unmeasured 2.

## Differences

None, so there are no domains to list and no rule readings to set against each other.

## Fixes to my own code after the comparison

None. The code and output were not changed after the study's tables were opened (the hashes above are from before
that).

## Internal cross-checks (not table cells)

- **Crawl status against the crawler's `done` marks.** My status for each pass agrees with the crawler's own mark in
  all 4,606 rows (3,557 domains in pass 1 and 1,049 in pass 2). Seven of the eight crawl-rule mismatches come from my
  own tests: 3 by the `ww<digits>` host (`encore-healthcare.com`, `mcep.us`, `umch.org`) and 4 by a parked marker. The
  eighth, `fitbit.com` (device), whose homepage redirects to `store.google.com`, is taken from the `done` mark (see
  case 7 below).
- **Identity.** For all 2,593 rows, the decision rule recomputed from the raters (agree, or else the tie-break)
  matches the stored `site` field. Of the 2,574 checked measured domains, 2,502 used a decision for the same pass and
  final URL. The other 72 used an earlier pass's decision: 23 because the homepage bytes were identical, and 49 because
  my extraction of the view found it equal. No measured domain was left without a decision.
- **Second parser.** `sectxt` 0.9.8 agrees with my parser on "has a valid contact" for all 184 chosen files of measured
  domains whose class is valid, valid with problems or invalid.
- **Refusal flag.** No stored row is unflagged where my reading of status and body says it was refused. Fifty-six rows
  are flagged `blocked` with no status or body marker (10 homepage rows, 1 at each `security.txt` location, the rest
  `robots.txt` and conventional paths). They are probably the header markers `cf-mitigated` or `x-amzn-waf-action`,
  which the store does not keep, so for those rows I relied on the stored flag.
- **Passes.** No measured domain has a `security.txt` location whose pass-1 and pass-2 results fall in different
  classes. The study's `passes_disagree` = 1 (hospital) also covers `cms-hpt.txt`, which I did not recompute.

## Cases where the protocol text was unclear, and how I read them

None of these changes a main or strict cell on this store. The column "Effect here" says why.

| # | Case | My reading | Effect here |
|---|---|---|---|
| 1 | "Body begins with an html, head, body or doctype tag": what if whitespace comes before the tag? | Leading whitespace and a BOM are ignored, so the body counts as HTML. | 2 responses (`text/plain`, CRLFs then `<!DOCTYPE html>`). HTML versus invalid; neither is positive or unmeasured, so k and n are unchanged. |
| 2 | "Expires must be a single date-time": what if the file has two `Expires` fields? | Exactly one field is required; with more than one, the file is valid with problems. | No file has more than one. |
| 3 | Contact validity: case of the scheme (`MAILTO:`); trailing text after the address. | The scheme is read case-insensitively. Any whitespace in the value makes it not valid. | No contact value in the store has an upper-case scheme or trailing text. All non-valid contacts are bare emails, which are lenient. |
| 4 | Field syntax: whitespace before the colon, or indented lines. | A name with no space before the colon is required; leading whitespace on a line is stripped. | No `Contact :` variant occurs. |
| 5 | One location is valid in one pass and valid with problems in the other. | The most recent result is used, because the "stands" exception only protects a positive result against a non-positive one. | No such location; strict k is unchanged. |
| 6 | "No site of the domain answered 429 before its crawl ended": when another listed domain's request got the 429. | Approximated by row order: a 429 row of the pass with an id at or below the domain's last row id. | Agrees with the crawler's marks in every row. |
| 7 | Third-party-platform crawl test. | The list lives in code (`domains.py`), which I did not open, so I took the crawler's `done` mark when my own two tests did not fire. | 1 domain (`fitbit.com`), in device mismatch / crawl rule. |
| 8 | Identity "view" equality when the latest measured pass's homepage is not the one decided. | Same view means equal host, title, `og:site_name`, description (each cut to 300) and the two text pieces, as my own HTML-to-text extraction gives them. | 49 domains rely on this. Each has an earlier decision and no later one, which is consistent with the study also finding the view unchanged. |
| 9 | Refusals by response header. | Headers are not stored, so I used the stored `blocked` flag. | 56 rows; see the cross-checks. |
| 10 | Expiry is judged "at the time the request was sent". For reused copies, `started_at` is when the copy was written. | I used `started_at`. | No parsed `Expires` falls within hours of either pass window, so the choice cannot matter. |
| 11 | Strict denominator. | The same as main (the domain's class is not unmeasured), because strict is defined on the same class. | None. |
| 12 | A `security.txt` request that redirects to a different file on the same site (`glenrosemedicalcenter.com`: the well-known address redirects to an uploaded `cms-hpt.txt`). | It is parsed as `security.txt`, has no contact, and is invalid. The protocol makes no exception for this. | Not positive and not unmeasured, so no effect. |

Two readings do matter for n, and the protocol text is explicit on both:
- **Plain 401/403 pairs.** 115 identified/browser 401/403 pairs are plain. The other 64 have a challenge marker (54)
  or a `<script` (10), so they stay unmeasured and leave the denominator (section 6.4, K5).
- **Never-requested locations.** A location never requested makes the domain unmeasured. For example,
  `northwell.edu`, `nuvancehealth.org` and `newmilfordhospital.org` all land on the `northwell.edu` site, whose limit
  of 12 non-homepage requests per pass was used up by other domains.

## Scope

Recomputed: the per-sector and all-domain status flow (listed, opted out, not crawled, measured, unreachable, blocked
with its rate-limited part, mismatch by reason) and the main and strict outcomes per listed domain with exact
intervals.

Not recomputed: policy page, listing and broad outcomes; per-hospital and bed-weighted shares; `cms-hpt.txt`; the
other flow-table columns (redirected off-domain, passes disagree, browser, forbidden files, the look's reasons, probes
cut, requests sent).


Note for the public snapshot: `recompute_output.json` (the per-domain output hashed above) is not included; it holds stored addresses of individual pages. The raw store this code reads is withheld.
