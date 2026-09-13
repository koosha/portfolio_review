"""Validate and normalize portfolio links collected by Chrome."""

from .storage import yahoo_navigation_url, yahoo_url


def discover_links(links):
    result = {}
    for link in links:
        try:
            url = yahoo_url(link["href"])
        except (ValueError, KeyError):
            continue
        name = link.get("text", "").strip()
        if name and url not in result:
            result[url] = dict(
                url=url, name=name[:200], navigation_url=yahoo_navigation_url(link["href"])
            )
    return list(result.values())
