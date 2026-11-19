from noroshi.robots import RobotsPolicy


def test_missing_or_garbage_robots_allows():
    assert RobotsPolicy(None).allowed("/security") and RobotsPolicy("<html>oops").allowed("/security")


def test_disallow_all_blocks_pages():
    assert not RobotsPolicy("User-agent: *\nDisallow: /").allowed("/security")


def test_own_group_wins_over_star():
    txt = "User-agent: *\nDisallow: /\n\nUser-agent: noroshi-research\nDisallow: /private"
    p = RobotsPolicy(txt)
    assert p.allowed("/security") and not p.allowed("/private/x")


def test_star_group_applies_when_no_own_group_and_query_kept():
    p = RobotsPolicy("User-agent: *\nDisallow: /admin")
    assert p.allowed("/security?x=1") and not p.allowed("/admin/page?x=1")


def test_longest_match_wins_over_file_order():
    p = RobotsPolicy("User-agent: *\nAllow: /\nDisallow: /security")
    assert not p.allowed("/security")


def test_wildcards_and_end_anchor():
    assert not RobotsPolicy("User-agent: *\nDisallow: /*security").allowed("/.well-known/security-policy")
    p = RobotsPolicy("User-agent: *\nDisallow: /*.pdf$")
    assert not p.allowed("/files/vdp.pdf") and p.allowed("/files/vdp.pdf.html")


def test_product_token_is_case_insensitive():
    assert not RobotsPolicy("User-agent: NOROSHI-Research\nDisallow: /").allowed("/x")


def test_disallow_all_constructor_blocks_every_path():
    p = RobotsPolicy.disallow_all()
    assert not p.allowed("/") and not p.allowed("/security")
