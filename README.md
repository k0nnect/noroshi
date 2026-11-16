# noroshi

a research study measuring how many healthcare organizations publish a way to report a security vulnerability.

## what the crawler does

- reads a few public files and pages per site: the homepage, robots.txt, /.well-known/security.txt, /security.txt, /cms-hpt.txt (hospitals only), and up to nine pages that might hold a vulnerability disclosure policy. one of those can be a page on another site that the homepage links to.
- GET requests only. no logins, no forms, no scanning, no testing for vulnerabilities. it reads what any visitor can read.
- at most 12 requests in a pass for each domain on the study's list, and at most 12 to any one site beyond homepage requests. about 1 MB of any response is read, no more.
- requests to one site are sent one at a time, at least 2 seconds apart, however many listed domains point to it.
- redirects are followed (up to 5 per request) and are not counted. a site that other listed domains redirect to therefore gets more than 12 requests in a pass: one homepage request for each listed domain that leads to it.
- no cookies are kept between requests.
- one pass, then one retry pass, at least 48 hours after a site's first request, for sites that could not be measured or were measured only in part.
- before the main pass, a pilot of about 150 sites is measured once to test the crawler, so those sites are visited one extra time.
- user agent: `noroshi-research/1.0 (+https://github.com/k0nnect/noroshi)`
- if a site refuses that user agent, the request is retried once with a standard browser user agent, and the rest of that site's requests in the pass use it. the paper reports this.
- if a site answers 429 (too many requests), nothing more is sent to that site for the rest of the pass.
- robots.txt: the homepage, robots.txt, security.txt and cms-hpt.txt are always requested (the last two exist to be read by third parties). every other page on the host the homepage ended on follows that host's robots.txt, as rfc 9309 describes. if that robots.txt cannot be read, no other page on that host is requested. a page the homepage links to on a different host is requested without reading that host's robots.txt.

## what gets published

aggregate results, and a per-domain table of what was found (file present or not, policy page found or not). contact details from security.txt files are never published.

## opt out

open an issue on this repository with your domain. once it is on the opt-out list, no run started after that sends it anything. everything collected from it is deleted, and it is left out of the findings. it stays in the list of domains the study started from, marked as opted out.

## who

christopher jagdeo, independent researcher.
