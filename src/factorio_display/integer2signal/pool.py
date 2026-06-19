"""Signal pool generation — produces the set of available Factorio item signals."""

from draftsman.data import items


def get_filtered_pool(reserved_signals: str | list[str] | None = None) -> list[str]:
    """Return available item signal names filtered by exclusions.

    Parameters
    ----------
    reserved_signals : str or list of str, optional
        Reserved signal names to exclude from the pool (e.g. the clock signal).

    Returns
    -------
    list[str]
        Filtered list of valid signal names.
    """
    full_pool = list(getattr(items, "raw", {}).keys())

    # Un-placeable, hidden, or meta signals
    exclude = ["signal-everything", "signal-anything", "signal-each"]

    if reserved_signals:
        if isinstance(reserved_signals, str):
            exclude.append(reserved_signals)
        else:
            exclude.extend(reserved_signals)

    # DLC / expansion patterns for hidden or un-placeable entities
    exclude_patterns = [
        "parameter",
        "space-platform",
        "cargo-pod",
    ]

    return [
        name
        for name in full_pool
        if name not in exclude
        and not any(pattern in name for pattern in exclude_patterns)
    ]
