"""
contentfilter.py — shared helpers for content-filtering config.

Content filtering is not one list (unlike L7). It is several fields:
    allowedUrlPatterns    : list[str]
    blockedUrlPatterns    : list[str]
    blockedUrlCategories  : list[str]   (category IDs on write)
    urlCategoryListSize   : "topSites" | "fullList"

Key gotcha (confirmed against the API): a GET returns blockedUrlCategories as
a list of OBJECTS like {"id": "meraki:contentFiltering/category/C1", "name": ...}
but the UPDATE wants a list of ID STRINGS. normalize_categories() handles that.
The category NAME is not needed on write. Because we propagate the GET output,
the category IDs are already in whatever format this org/firmware expects.
"""

# The fields we carry for a full content-filtering state.
FIELDS = ["allowedUrlPatterns", "blockedUrlPatterns", "blockedUrlCategories", "urlCategoryListSize"]


def normalize_categories(categories):
    """
    Turn blockedUrlCategories into the list-of-ID-strings the UPDATE wants.
    Accepts either the GET's list-of-objects or an already-flat list of strings.
    """
    if not categories:
        return []
    if isinstance(categories[0], dict):
        return [c["id"] for c in categories]
    return list(categories)


def normalize_settings(raw):
    """
    Take a raw GET result (or a loaded export) and return a dict with exactly the
    four updatable fields, with categories flattened to IDs. Missing fields are
    omitted so we never send keys the network didn't have.
    """
    out = {}
    if "allowedUrlPatterns" in raw:
        out["allowedUrlPatterns"] = list(raw.get("allowedUrlPatterns", []))
    if "blockedUrlPatterns" in raw:
        out["blockedUrlPatterns"] = list(raw.get("blockedUrlPatterns", []))
    if "blockedUrlCategories" in raw:
        out["blockedUrlCategories"] = normalize_categories(raw.get("blockedUrlCategories", []))
    if raw.get("urlCategoryListSize"):
        out["urlCategoryListSize"] = raw["urlCategoryListSize"]
    return out


def merge_settings(current, source):
    """
    APPEND mode: union the URL pattern lists and category lists (dedup, order
    preserved: existing first, then new). urlCategoryListSize is taken from the
    source if present, else kept from current.
    Both inputs should already be normalized.
    """
    def union(a, b):
        seen, out = set(), []
        for x in list(a) + list(b):
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    merged = {
        "allowedUrlPatterns": union(current.get("allowedUrlPatterns", []),
                                    source.get("allowedUrlPatterns", [])),
        "blockedUrlPatterns": union(current.get("blockedUrlPatterns", []),
                                    source.get("blockedUrlPatterns", [])),
        "blockedUrlCategories": union(current.get("blockedUrlCategories", []),
                                      source.get("blockedUrlCategories", [])),
    }
    size = source.get("urlCategoryListSize") or current.get("urlCategoryListSize")
    if size:
        merged["urlCategoryListSize"] = size
    return merged


def summarize(settings):
    """Short one-line count summary for previews."""
    return (f"blockedUrls={len(settings.get('blockedUrlPatterns', []))} "
            f"allowedUrls={len(settings.get('allowedUrlPatterns', []))} "
            f"categories={len(settings.get('blockedUrlCategories', []))}")
