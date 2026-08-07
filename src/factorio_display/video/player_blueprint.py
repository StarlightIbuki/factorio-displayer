"""Display blueprint builder — generates a Factorio lamp-display blueprint string."""

from draftsman.blueprintable import Blueprint
from draftsman.entity import new_entity

# pylint: disable=relative-beyond-top-level — valid intra-package imports
from .. import (
    CLOCK_SIGNAL,
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    QUALITIES,
    SIGNAL_POOL,
)
from ..integer2signal.mapping import SignalMapping, compute_chunking
from ..logical_blueprint import Endpoint, LogicalBlueprint, LogicalEntity
# pylint: enable=relative-beyond-top-level


def build_display(  # pylint: disable=too-many-locals
    name: str = "Video Display",
    width: int | None = None,
    height: int | None = None,
) -> Blueprint:
    """Build a lamp-grid display Blueprint.

    Always generates the display dynamically — no pre-computed blueprint
    is used.  Custom dimensions produce a fresh blueprint.

    No power poles or substations are placed — the user supplies power in-game.

    Returns a :class:`~draftsman.blueprintable.Blueprint`.
    """
    from ..logical_blueprint import to_draftsman
    lb = build_display_logical(name=name, width=width, height=height)
    return to_draftsman(lb)


def build_display_logical(  # pylint: disable=too-many-locals
    name: str = "Video Display",
    width: int | None = None,
    height: int | None = None,
    *,
    connectors: bool = False,
) -> LogicalBlueprint:
    """Build a lamp-grid display LogicalBlueprint.

    If the display has more pixels than the signal pool can address, the
    display is split into disconnected vertical chunks, each with its own
    signal mapping and independent red-wire bus.

    When *connectors* is True (split-output mode), each chunk also gets a
    constant-combinator connector on the red data bus plus a non-wired
    series-label CC (see :func:`_build_display_chunked`).

    Returns a :class:`LogicalBlueprint` with ``input_ports={"data_0": ...,
    "data_1": ...}`` (one port per chunk) for the colour signal bus.
    For single-chunk displays the port is named ``"data"`` for backwards
    compatibility.
    """
    w = width if width is not None else DISPLAY_WIDTH
    h = height if height is not None else DISPLAY_HEIGHT

    pool = SIGNAL_POOL
    qualities = QUALITIES

    chunk_height, num_chunks = compute_chunking(w, h, pool, qualities)

    return _build_display_chunked(
        name=name, width=w, height=h,
        chunk_height=chunk_height, num_chunks=num_chunks,
        pool=pool, qualities=qualities,
        connectors=connectors,
    )


def _build_display_chunked(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    name: str,
    width: int,
    height: int,
    chunk_height: int,
    num_chunks: int,
    pool: list[str],
    qualities: list[str],
    *,
    connectors: bool = False,
) -> LogicalBlueprint:
    """Build a chunked lamp-grid display LogicalBlueprint.

    Each chunk is a self-contained lamp grid with its own SignalMapping
    and independent red-wire bus.  Chunks are placed at increasing Y
    offsets so they stack vertically.

    When *connectors* is True (split-output mode), every chunk also gets:
      * a constant-combinator "connector" to the right of the lamp grid,
        wired into the red data bus and carrying the chunk's identifying
        signal at value 1 with the CC "Output" toggle OFF (visible on the
        map, but never emitted onto the bus) — the user wires the matching
        memory fragment's connector to it;
      * a non-wired series-label CC noting the chunk index (1-based).
    """
    lb = LogicalBlueprint(label=name)
    lb.icon = "display-panel"  # show a display-panel icon in the blueprint book

    cum_y = 0
    margin = 0  # lamps are 1x1, no overlap risk

    for ci in range(num_chunks):
        y0 = ci * chunk_height
        y1 = min(y0 + chunk_height, height)
        ch_h = y1 - y0
        ch_w = width

        mapping = SignalMapping(ch_w, ch_h, qualities, pool)

        # Build lamp grid for this chunk, offset by cum_y
        lamp_grid: list[list[str | None]] = [[None for _ in range(ch_w)] for _ in range(ch_h)]

        for (x, y), sig in mapping.iter_pixels():
            lamp_id = f"lamp_c{ci}_{x}_{y}"
            sig_str = sig["name"]
            if sig.get("quality") and sig["quality"] != "normal":
                sig_str = f"{sig['name']}@{sig['quality']}"
            lamp = LogicalEntity(
                lamp_id,
                "small-lamp",
                properties={
                    "use_colors": True,
                    "always_on": True,
                    "circuit_enabled": False,
                    "color_signal": sig_str,
                },
                position=(x, y + cum_y),
            )
            lb.add_entity(lamp)
            lamp_grid[y][x] = lamp_id

        # Horizontal wiring — chain lamps across each row
        for py in range(ch_h):
            for px in range(ch_w - 1):
                curr_id = lamp_grid[py][px]
                next_id = lamp_grid[py][px + 1]
                if curr_id and next_id:
                    lb.connect("red", Endpoint(curr_id, "input"),
                              Endpoint(next_id, "input"))

        # Vertical wiring at the rightmost column
        for py in range(ch_h - 1):
            if lamp_grid[py][ch_w - 1] and lamp_grid[py + 1][ch_w - 1]:
                lb.connect("red", Endpoint(lamp_grid[py][ch_w - 1], "input"),
                          Endpoint(lamp_grid[py + 1][ch_w - 1], "input"))

        # ── Connector CC (split mode): right of the lamp chunk, on the red bus ──
        # Carries the chunk's identifying signal at value 1 with the CC
        # "Output" toggle OFF (visible on the map, no numeric pollution).
        # Wired into the lamp network so it is a usable in-game connection
        # point to the matching memory fragment.
        conn_cc_id: str | None = None
        conn_anchor: str | None = None
        if connectors:
            sig0 = mapping.get_signal(0, 0)
            label_sig: str | None = None
            if sig0:
                label_sig = sig0["name"]
                if sig0.get("quality") and sig0["quality"] != "normal":
                    label_sig = f"{sig0['name']}@{sig0['quality']}"
            conn_cc_id = f"cc_c{ci}_data"
            # The connector carries the chunk's identifying signal at value 1
            # with the CC "Output" toggle OFF (enabled=False) — visible on the
            # map as a label, but never emitted onto the data bus.
            conn_props: dict = {"enabled": False}
            if label_sig:
                conn_props["signals"] = [{"name": label_sig, "value": 1}]
            lb.add_entity(LogicalEntity(
                conn_cc_id, "constant-combinator",
                properties=conn_props,
                position=(ch_w, cum_y),
            ))
            # Non-wired series-label CC (chunk index + 1 so it shows on the map).
            lb.add_entity(LogicalEntity(
                f"cc_c{ci}_label", "constant-combinator",
                properties={
                    "signals": [{"name": "signal-info", "value": ci + 1}],
                    "enabled": False,
                },
                position=(ch_w, cum_y + 1),
            ))
            # Join the red data bus (network membership).  The connector sits
            # at x=ch_w, one tile right of the top row, so it must be wired to
            # the *nearest* lamp — the rightmost lamp of the top row.  Wiring
            # to the top-LEFT lamp (x=0) would span the whole chunk width and
            # exceed Factorio's 9-tile circuit-wire reach, silently dropping
            # the wire and leaving the connector disconnected.
            for px in range(ch_w - 1, -1, -1):
                if lamp_grid[0][px]:
                    conn_anchor = lamp_grid[0][px]
                    break
            if conn_anchor is None:
                # Degenerate/partial top row — fall back to any lamp.
                for row in lamp_grid:
                    for lid in row:
                        if lid:
                            conn_anchor = lid
                            break
                    if conn_anchor:
                        break
            if conn_anchor:
                lb.connect("red", Endpoint(conn_cc_id, "input"),
                           Endpoint(conn_anchor, "input"))

        # Declare data input port for this chunk
        port_name = "data" if num_chunks == 1 else f"data_{ci}"
        if lamp_grid[0][0]:
            for net in lb.networks:
                if net.color == "red" and Endpoint(lamp_grid[0][0], "input") in net.endpoints:
                    lb.set_input_port(port_name, net.network_id)
                    # ── Pre-assign wire pairs for the lamp grid ──────
                    pairs: list[tuple[Endpoint, Endpoint]] = []
                    for py in range(ch_h):
                        for px in range(ch_w - 1):
                            if lamp_grid[py][px] and lamp_grid[py][px + 1]:
                                pairs.append((
                                    Endpoint(lamp_grid[py][px], "input"),
                                    Endpoint(lamp_grid[py][px + 1], "input"),
                                ))
                    for py in range(ch_h - 1):
                        if lamp_grid[py][ch_w - 1] and lamp_grid[py + 1][ch_w - 1]:
                            pairs.append((
                                Endpoint(lamp_grid[py][ch_w - 1], "input"),
                                Endpoint(lamp_grid[py + 1][ch_w - 1], "input"),
                            ))
                    if conn_cc_id is not None and conn_anchor is not None:
                        pairs.append((
                            Endpoint(conn_cc_id, "input"),
                            Endpoint(conn_anchor, "input"),
                        ))
                    net.prewired_pairs = pairs
                    break

        # Advance Y offset for next chunk
        cum_y += ch_h + margin

    return lb
