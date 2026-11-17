# noroshi

a research study measuring how many healthcare organizations publish a way to report a security vulnerability.

## what the crawler does

- reads a few public files and pages per site: the homepage, robots.txt, /.well-known/security.txt, /security.txt, /cms-hpt.txt (hospitals only), and up to nine pages that might hold a vulnerability disclosure policy. some of those can be pages on another site that the homepage or the security.txt file links to.
- GET requests only. no logins, no forms, no scanning, no testing for vulnerabilities. it reads what any visitor can read.
- at most 12 requests in a pass for each domain on the study's list, and, beyond homepage requests, at most 12 requests made to addresses on any one site. about 4 MB of a homepage and about 1 MB of any other response is read, no more.
- requests to one site are sent one at a time, at least 2 seconds apart, however many listed domains point to it.
- redirects are followed (up to 5 per request) and count only at the site the request was made to. a site that other listed domains or their pages redirect to, or that several listed domains' homepages lead to, can therefore get more than 12 requests in a pass.
- if a homepage address gives no answer at all (no such name, no connection, a certificate error or a timeout), the same name with or without `www.` is tried once.
- a page with almost no text that forwards the browser with a meta refresh is followed once, to that address.
- no cookies are kept between requests.
- one pass, then one retry pass, at least 48 hours after a site's first request, for sites that could not be measured or were measured only in part.
- before the main pass, the crawler is tested in two pilot rounds of about 150 listed domains each, with no domain in both. those sites are visited one extra time (a site that domains in both rounds lead to, twice). the first round ran on 16 november 2026.
- user agent: `noroshi-research/1.0 (+https://github.com/k0nnect/noroshi)`
- if a site refuses that user agent, the request is retried once with a standard browser user agent. if that retry gets an answer, the rest of the site's requests in the pass use the browser user agent; if not, they keep the study's. the paper reports this.
- if a site answers 429 (too many requests), nothing more is sent to that site for the rest of the pass.
- robots.txt: the homepage, robots.txt, security.txt and cms-hpt.txt are requested without reading robots.txt (the last two exist to be read by third parties). every other page on the host the homepage ended on follows that host's robots.txt (rfc 9309), with one stricter rule: if that robots.txt cannot be read or is refused, no other page on that host is requested. a page on a different host (one the homepage or the security.txt file links to, or a meta refresh leads to) is requested without reading that host's robots.txt.

## what gets published

aggregate results, and a per-domain table of what was found (file present or not, policy page found or not). contact details from security.txt files are never published.

## opt out

open an issue on this repository with your domain. once it is on the opt-out list, no run started after that sends it anything. everything collected from it is deleted, and it is left out of the findings. it stays in the list of domains the study started from, and the published per-domain table marks it as opted out.

## who

christopher jagdeo, independent researcher.
