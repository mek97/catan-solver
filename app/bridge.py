"""Translating a live position into catanatron, so a real engine can judge it.

catanatron (github.com/bcollazo/catanatron) is a mature open-source Catan
engine with searching players -- AlphaBeta, MCTS, playouts -- on top of a value
function tuned by self-play. Our solver evaluates one ply and has never been
measured against anything. Handing it our position is how it gets measured, and
how a stronger recommendation can be asked for.

The whole difficulty is addressing. Their board is cube coordinates with six
named corners per tile; ours is axial with a vertex key per corner. Both label
corners in the same rotational order, and the correspondence turns out to be

    their (x, y, z) -> our (q, r) = (x, z)
    their NORTH, NORTHEAST, ... -> our N, NE, ...

which was not assumed: every rotation and reflection was tried, and a mapping
is kept only if each of their 54 nodes lands on exactly one of our vertices
from every tile that touches it. Twelve orientations pass, the board being
symmetric; this is the one that needs no rotation.

A wrong mapping here would not crash. It would quietly evaluate a different
board, so `verify()` checks the translation against the position it came from
rather than trusting the arithmetic.
"""
from __future__ import annotations

from typing import Any, Optional

from catanatron import Color
from catanatron.models.map import BASE_MAP_TEMPLATE, CatanMap

from . import board

# their resource names, against ours
RESOURCE_OUT = {
    "wood": "WOOD", "brick": "BRICK", "sheep": "SHEEP",
    "wheat": "WHEAT", "ore": "ORE", "desert": None,
}
# Their palette is RED, BLUE, ORANGE, WHITE; colonist deals green as its
# fourth. Mapping green to nothing silently dropped that player, and a missing
# player is not a smaller game -- their settlements are what cut everyone
# else's roads, so every longest road came out wrong.
COLOR_OUT = {
    "red": Color.RED, "blue": Color.BLUE,
    "orange": Color.ORANGE, "green": Color.WHITE,
}
MAX_SEATS = len(COLOR_OUT)

# catanatron is a four-player, nineteen-hex engine: four colours, and a base
# map whose land is the standard hexagon. It has no 5-6 player extension, so a
# thirty-hex board or a fifth seat cannot cross over -- which is a limit of
# theirs, not a gap in the translation, and worth saying rather than failing
# obscurely halfway through building a state.
def supported(cfg) -> Optional[str]:
    """Why this position cannot be translated, or None if it can."""
    if len(cfg.hexes) != 19:
        return f"catanatron plays the 19-hex board; this one has {len(cfg.hexes)}"
    if len(cfg.players) > MAX_SEATS:
        return f"catanatron seats {MAX_SEATS} players; this game has {len(cfg.players)}"
    unknown = sorted(set(cfg.players) - set(COLOR_OUT))
    if unknown:
        return f"no catanatron colour for {', '.join(unknown)}"
    return None


def _fresh_map() -> CatanMap:
    return CatanMap.from_template(BASE_MAP_TEMPLATE)


def hex_mapping(catan_map: CatanMap) -> dict[tuple, int]:
    """Their cube coordinate -> our hex id."""
    out = {}
    for cube in catan_map.land_tiles:
        q, r = cube[0], cube[2]
        hid = board.BASE.HEX_ID.get((q, r))
        if hid is not None:
            out[cube] = hid
    return out


def node_mapping(catan_map: CatanMap) -> dict[int, int]:
    """Their node id -> our vertex id.

    Built from every tile and checked for agreement: a node shared by three
    tiles has to arrive at the same vertex from all three, which is what makes
    this a proof rather than a coincidence.
    """
    hexes = hex_mapping(catan_map)
    out: dict[int, int] = {}
    for cube, tile in catan_map.land_tiles.items():
        ours = board.BASE.HEX_VERTICES[hexes[cube]]
        for i, ref in enumerate(tile.nodes):
            theirs = tile.nodes[ref]
            mine = ours[i]
            if out.setdefault(theirs, mine) != mine:
                raise ValueError(
                    f"node {theirs} maps to both {out[theirs]} and {mine}"
                )
    if len(out) != len(board.BASE.VERTICES):
        raise ValueError(f"mapped {len(out)} nodes, expected {len(board.BASE.VERTICES)}")
    return out


def edge_mapping(node_map: dict[int, int]) -> dict[tuple[int, int], int]:
    """Their (node, node) edge -> our edge id.

    Their edges are node pairs and ours are dense ids, so this is the node
    mapping read backwards: an edge is whichever of ours joins the two vertices
    their nodes translate to.
    """
    ours = {
        tuple(sorted((a, b))): eid
        for eid, (a, b) in enumerate(board.BASE.EDGE_VERTICES)
    }
    out = {}
    for a_theirs, a_ours in node_map.items():
        for b_theirs, b_ours in node_map.items():
            if a_theirs >= b_theirs:
                continue
            eid = ours.get(tuple(sorted((a_ours, b_ours))))
            if eid is not None:
                out[(a_theirs, b_theirs)] = eid
    return out


def dress_map(cfg, catan_map: Optional[CatanMap] = None) -> CatanMap:
    """A catanatron map carrying *our* tiles: same shape, our resources and numbers."""
    catan_map = catan_map or _fresh_map()
    for cube, hid in hex_mapping(catan_map).items():
        tile = catan_map.land_tiles[cube]
        ours = cfg.hexes[hid]
        tile.resource = RESOURCE_OUT[ours.resource]
        tile.number = ours.number
    return catan_map


def to_state(cfg):
    """A catanatron State holding our position.

    Built through their own build_settlement / build_city / build_road rather
    than by writing into the structures underneath, because those keep derived
    things -- connected components, road lengths, who holds the longest -- in
    step. Setting the pieces directly would leave a State that looks right and
    answers questions wrong.

    Roads are placed in whatever order connectivity allows: theirs validates
    that a road touches your network, and ours arrives as an unordered set.
    """
    from catanatron.models.player import Player as _Player
    from catanatron.state import State
    from catanatron.state_functions import player_key

    why = supported(cfg)
    if why:
        raise ValueError(why)

    nodes = {v: k for k, v in node_mapping(dress_map(cfg)).items()}   # ours -> theirs
    catan_map = dress_map(cfg)
    seats = [c for c in cfg.players if c in COLOR_OUT]
    players = [_Player(COLOR_OUT[c]) for c in seats]
    state = State(players, catan_map)

    for color in seats:
        theirs = COLOR_OUT[color]
        p = cfg.players[color]
        for v in p.settlements:
            state.board.build_settlement(theirs, nodes[v], initial_build_phase=True)
        for v in p.cities:
            state.board.build_settlement(theirs, nodes[v], initial_build_phase=True)
            state.board.build_city(theirs, nodes[v])

    pending = [(COLOR_OUT[c], e) for c in seats for e in cfg.players[c].roads]
    while pending:
        progressed = False
        for item in list(pending):
            theirs, eid = item
            a, b = board.BASE.EDGE_VERTICES[eid]
            try:
                state.board.build_road(theirs, tuple(sorted((nodes[a], nodes[b]))))
            except ValueError:
                continue          # not connected yet; another road comes first
            pending.remove(item)
            progressed = True
        if not progressed:
            break                 # whatever is left cannot be reached from here

    # A freshly built State is at the opening, where settlements are free and
    # cost nothing to reach. Left that way its "legal moves" are 54 free
    # placements and any engine asked about them answers BUILD_SETTLEMENT, in
    # every position, however empty the hand -- a comparison that looks like it
    # ran and means nothing.
    if cfg.phase == "main":
        from catanatron.state import ActionPrompt

        state.is_initial_build_phase = False
        state.current_prompt = ActionPrompt.PLAY_TURN

    _fill_players(state, cfg, seats)
    # Their longest-road length is cached in player_state and maintained by
    # their state-level build path, which building through the board bypasses.
    # Left unset it reads zero for everybody, and every feature that leans on
    # it -- including who holds the trophy -- is quietly wrong.
    for color in seats:
        theirs = COLOR_OUT[color]
        paths = state.board.continuous_roads_by_player(theirs)
        longest = max((len(p) for p in paths), default=0)
        state.player_state[f"{player_key(state, theirs)}_LONGEST_ROAD_LENGTH"] = longest
    state.board.robber_coordinate = _robber_cube(cfg, catan_map)
    return state


def _robber_cube(cfg, catan_map: CatanMap):
    for cube, hid in hex_mapping(catan_map).items():
        if hid == cfg.robber_hex:
            return cube
    return next(iter(catan_map.land_tiles))


def _fill_players(state, cfg, seats) -> None:
    """Hands, cards and points, in the flat keys their features read."""
    from catanatron.state_functions import player_key

    for color in seats:
        p = cfg.players[color]
        key = player_key(state, COLOR_OUT[color])
        mine = color == cfg.me.color
        hand = cfg.me.hand if mine else {}
        for r in ("wood", "brick", "sheep", "wheat", "ore"):
            state.player_state[f"{key}_{RESOURCE_OUT[r]}_IN_HAND"] = hand.get(r, 0)
        if not mine:
            # their composition is hidden; the count is not
            each, extra = divmod(p.resource_count, 5)
            for i, r in enumerate(("wood", "brick", "sheep", "wheat", "ore")):
                state.player_state[f"{key}_{RESOURCE_OUT[r]}_IN_HAND"] = each + (1 if i < extra else 0)
        dev = cfg.me.dev_cards if mine else None
        state.player_state[f"{key}_KNIGHT_IN_HAND"] = dev.knight if dev else 0
        state.player_state[f"{key}_ROAD_BUILDING_IN_HAND"] = dev.road_building if dev else 0
        state.player_state[f"{key}_YEAR_OF_PLENTY_IN_HAND"] = dev.year_of_plenty if dev else 0
        state.player_state[f"{key}_MONOPOLY_IN_HAND"] = dev.monopoly if dev else 0
        state.player_state[f"{key}_VICTORY_POINT_IN_HAND"] = dev.vp if dev else 0
        # whether this player has already rolled: ours says so through the
        # pending state, and without it their engine offers only ROLL
        if mine:
            state.player_state[f"{key}_HAS_ROLLED"] = cfg.pending != "roll"
        state.player_state[f"{key}_PLAYED_KNIGHT"] = p.knights_played
        state.player_state[f"{key}_HAS_ARMY"] = p.largest_army
        state.player_state[f"{key}_HAS_ROAD"] = p.longest_road
        vp = len(p.settlements) + 2 * len(p.cities) + (2 if p.longest_road else 0) + (2 if p.largest_army else 0)
        state.player_state[f"{key}_VICTORY_POINTS"] = max(vp, p.vp_visible)
        state.player_state[f"{key}_ACTUAL_VICTORY_POINTS"] = (
            max(vp, p.vp_visible) + (dev.vp if dev else 0)
        )


def _playable_actions(state):
    """Their legal-move generator, wherever this version keeps it.

    It moved between catanatron.state and catanatron.models.actions, and the
    published package and the source tree are on opposite sides of that. The
    strong players only exist in the latter, so both have to work.
    """
    try:
        from catanatron.models.actions import generate_playable_actions
    except ImportError:
        from catanatron.state import generate_playable_actions
    return generate_playable_actions(state)


def best_action(cfg, player=None, depth: int = 3):
    """What a searching engine would play in this position.

    Defaults to alpha-beta over their tuned value function, which measured at a
    median of 9ms and a worst case under half a second -- comfortably inside a
    turn timer, and the reason MCTS is not the default despite being the
    better-known name: at a hundred simulations it is both slower and weaker.
    """
    from catanatron import Game

    why = supported(cfg)
    if why:
        raise ValueError(why)

    state = to_state(cfg)
    me = COLOR_OUT[cfg.me.color]
    # Only the index: current_color is a method in some versions and an
    # attribute in others, and assigning it shadows the method where it is one.
    state.current_player_index = list(state.colors).index(me)
    if not callable(getattr(state, "current_color", None)):
        state.current_color = me

    if player is None:
        from catanatron.players.minimax import AlphaBetaPlayer

        player = AlphaBetaPlayer(me, depth, True)

    # A fully-built Game, with our position swapped in. Constructing an empty
    # shell and adding attributes as each one is missed chases a moving target:
    # seed, playable_actions and the rest differ between versions, and the
    # searcher reaches for all of them.
    from catanatron.models.player import Player as _P

    game = Game([_P(c) for c in state.colors], catan_map=state.board.map)
    game.state = state
    actions = _playable_actions(state)
    game.state.playable_actions = actions
    if hasattr(game, "playable_actions"):
        game.playable_actions = actions
    return player.decide(game, actions), actions


def verify(cfg) -> dict[str, Any]:
    """Check the translation describes the same board we started from.

    A mapping that is subtly wrong evaluates a different game and says nothing
    about it, so this compares what each side believes: which resource and
    number sit on each tile, and which tiles each corner touches.
    """
    catan_map = dress_map(cfg)
    hexes = hex_mapping(catan_map)
    nodes = node_mapping(catan_map)

    tiles_ok = all(
        catan_map.land_tiles[cube].number == cfg.hexes[hid].number
        and catan_map.land_tiles[cube].resource == RESOURCE_OUT[cfg.hexes[hid].resource]
        for cube, hid in hexes.items()
    )

    # every corner should touch the same tiles on both sides
    corners_ok = True
    for cube, tile in catan_map.land_tiles.items():
        for ref in tile.nodes:
            theirs = tile.nodes[ref]
            mine = nodes[theirs]
            if hexes[cube] not in board.BASE.VERTEX_HEXES[mine]:
                corners_ok = False
    return {
        "hexes": len(hexes),
        "nodes": len(nodes),
        "tiles_match": tiles_ok,
        "corners_match": corners_ok,
        "ok": tiles_ok and corners_ok and len(hexes) == 19 and len(nodes) == 54,
    }
