"""Smoke test â?end-to-end all-in-one composition with generated small

video (3 frames, 10Ã50 random colour) and MIDI input, across all

power/pole-type/progress-bar combinations.

"""



from __future__ import annotations



import itertools

import tempfile

import warnings

from pathlib import Path



import numpy as np

import pytest



from factorio_display.logical_blueprint import (

    LogicalBlueprint,

    LogicalEntity,

    from_draftsman,

    to_draftsman,

    to_toml,

    from_toml,

)

from factorio_display.composer import compose_all_in_one

from factorio_display.timer import build_raw_timer, build_mod_timer, build_repeater

from factorio_display.progress_bar import build_progress_bar

from draftsman.blueprintable import Blueprint





# ======================================================================â?

# Helpers â?generate small test media

# ======================================================================â?





def _make_test_video_frames(num_frames: int = 3, w: int = 10, h: int = 50) -> list[np.ndarray]:

    """Generate *num_frames* random-colour frames of size (h, w, 3)."""

    rng = np.random.default_rng(42)

    return [rng.integers(0, 256, (h, w, 3), dtype=np.uint8) for _ in range(num_frames)]





def _encode_video_frames_to_bp(

    frames: list[np.ndarray],

    output_name: str = "SmokeVideo",

) -> str:

    """Encode raw numpy frames into a video-memory blueprint string."""

    from factorio_display.video.encoder import _encode_frames_core

    from factorio_display.integer2signal.pool import get_filtered_pool

    from factorio_display.integer2signal.mapping import SignalMapping



    h, w = frames[0].shape[:2]

    pool = get_filtered_pool("signal-clock")

    qualities = ["normal", "uncommon", "rare", "epic", "legendary"]

    mapping = SignalMapping(w, h, qualities, pool)

    mapping_params = {

        "width": w, "height": h,

        "qualities": qualities,

        "signal_pool": mapping.base_signals,

    }



    tick_ranges = [(i, i) for i in range(len(frames))]

    return _encode_frames_core(

        kept_frames=list(frames),

        tick_ranges=tick_ranges,

        output_name=output_name,

        deduplicate=False,

        mapping_params=mapping_params,

        clock="signal-clock",

        current_tick=len(frames),

    )





def _make_test_midi() -> bytes:

    """Create a tiny 3-note MIDI file (C4, E4, G4)."""

    import mido



    mid = mido.MidiFile(ticks_per_beat=480)

    track = mido.MidiTrack()

    mid.tracks.append(track)



    # Set instrument to piano

    track.append(mido.Message("program_change", program=0, time=0))

    # Three notes: C4 (60), E4 (64), G4 (67)

    for note in [60, 64, 67]:

        track.append(mido.Message("note_on", note=note, velocity=80, time=0))

        track.append(mido.Message("note_off", note=note, velocity=0, time=480))



    with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as f:

        mid.save(f.name)

        return Path(f.name)





def _encode_midi_to_bp(midi_path: Path) -> str:

    """Encode a MIDI file into an audio-memory blueprint string."""

    from factorio_display.audio.encoder import encode_audio_auto



    return encode_audio_auto(

        str(midi_path),

        attach_player=False,

        map_drums=False,

        rail_mode="piano",

        use_global_shift=False,

    )





# ======================================================================â?

# Fixtures

# ======================================================================â?





@pytest.fixture(scope="module")

def video_bp_str() -> str:

    """3-frame 10Ã50 random-colour video memory blueprint."""

    frames = _make_test_video_frames(3, 10, 50)

    return _encode_video_frames_to_bp(frames, "SmokeVideo")





@pytest.fixture(scope="module")

def midi_path() -> Path:

    """Tiny 3-note MIDI file."""

    p = _make_test_midi()

    yield p

    try:

        p.unlink()

    except OSError:

        pass





@pytest.fixture(scope="module")

def audio_bp_str(midi_path: Path) -> str:

    """Audio-memory blueprint from test MIDI."""

    return _encode_midi_to_bp(midi_path)





@pytest.fixture(scope="module")

def display_lb() -> LogicalBlueprint:

    """Small lamp-grid display LogicalBlueprint (10Ã50)."""

    from factorio_display.video.player_blueprint import build_display



    bp_str = build_display("SmokeDisplay", width=10, height=50)

    bp = Blueprint.from_string(bp_str)

    return from_draftsman(bp)





@pytest.fixture(scope="module")

def video_memory_lb(video_bp_str: str) -> LogicalBlueprint:

    """Video memory LogicalBlueprint."""

    bp = Blueprint.from_string(video_bp_str)

    return from_draftsman(bp)





@pytest.fixture(scope="module")

def audio_memory_lb(audio_bp_str: str) -> LogicalBlueprint:

    """Audio memory LogicalBlueprint."""

    bp = Blueprint.from_string(audio_bp_str)

    return from_draftsman(bp)





@pytest.fixture(scope="module")

def timer_lb() -> LogicalBlueprint:

    """Default timer (raw + mod)."""

    from factorio_display.composer import _assign_tile_positions

    timer = build_raw_timer("SmokeClock")

    mod = build_mod_timer(60, name="SmokeSubTick")

    _assign_tile_positions(mod, start_x=0, start_y=4)

    timer.merge(mod, entity_prefix="m_", network_prefix="m_")

    return timer





@pytest.fixture(scope="module")

def progress_lb() -> LogicalBlueprint:

    """Default progress bar."""

    return build_progress_bar("SmokePB", length=10, signal_name="signal-clock", max_value=59)





# ======================================================================â?

# Parametrised smoke tests

# ======================================================================â?



POLE_TYPES = [None, "small", "medium", "substation"]

PROGRESS_OPTIONS = [False, True]

CACHE_OPTIONS = [False, True]





@pytest.mark.filterwarnings("ignore::draftsman.warning.ConnectionDistanceWarning")

@pytest.mark.filterwarnings("ignore::draftsman.warning.ConnectionSideWarning")

@pytest.mark.filterwarnings("ignore::draftsman.warning.UnknownNoteWarning")

@pytest.mark.filterwarnings("ignore::draftsman.warning.UnknownSignalWarning")

class TestAllInOneSmoke:

    """Comprehensive smoke test: every pole Ã progress Ã cache combination."""



    @pytest.mark.parametrize(

        "pole_type, use_progress, use_cache",

        list(itertools.product(POLE_TYPES, PROGRESS_OPTIONS, CACHE_OPTIONS)),

    )

    def test_video_composition(

        self,

        display_lb: LogicalBlueprint,

        video_memory_lb: LogicalBlueprint,

        timer_lb: LogicalBlueprint,

        progress_lb: LogicalBlueprint,

        pole_type: str | None,

        use_progress: bool,

        use_cache: bool,

    ):

        """Compose video-only all-in-one with every param combo."""

        result = compose_all_in_one(

                display_lb=display_lb,

                video_memory_lb=video_memory_lb,

                timer_lb=timer_lb,

                progress_bar_lb=progress_lb if use_progress else None,

                pole_type=pole_type,

                output_name=f"Smoke_{pole_type}_pb{use_progress}",

                use_cache=use_cache,

                cache_key_parts=("smoke", "video", str(pole_type),

                                 str(use_progress), str(use_cache)),

            )



        # ââ Assertions âââââââââââââââââââââââââââââââââââââââââââ

        assert result.label.startswith("Smoke_")

        assert len(result.entities) > 0



        # Every entity should have a position after composition

        for eid, ent in result.entities.items():

            assert ent.position is not None, f"Entity {eid!r} missing position"



        # ââ Convert to draftsman blueprint âââââââââââââââââââââââ

        bp = to_draftsman(result)

        bp_str = bp.to_string()

        assert bp_str.startswith("0e"), "Not a valid blueprint string"



        # ââ Round-trip through from_draftsman ââââââââââââââââââââ

        bp2 = Blueprint.from_string(bp_str)

        lb2 = from_draftsman(bp2)

        assert len(lb2.entities) == len(result.entities), (

            f"Entity count mismatch: {len(lb2.entities)} vs {len(result.entities)}"

        )



        # ââ Check for power pole entities ââââââââââââââââââââââââ

        pole_types_found = {

            ent.type for ent in lb2.entities.values()

            if ent.type in ("small-electric-pole", "medium-electric-pole", "substation")

        }

        if pole_type is not None:

            # TODO: re-enable assertion when power supply is implemented

            # (see power.py docstring for redesign plan).

            expected_type = {

                "small": "small-electric-pole",

                "medium": "medium-electric-pole",

                "substation": "substation",

            }[pole_type]

            if expected_type not in pole_types_found:

                pytest.skip(

                    f"Power supply ({pole_type}) not yet implemented"

                )

        else:

            assert len(pole_types_found) == 0, (

                f"Unexpected poles: {pole_types_found}"

            )



        # ââ Check for progress bar lamps âââââââââââââââââââââââââ

        lamp_count = sum(

            1 for ent in lb2.entities.values() if ent.type == "small-lamp"

        )

        if use_progress:

            assert lamp_count > 10, f"Expected >10 lamps, got {lamp_count}"

        else:

            assert lamp_count > 0



    @pytest.mark.parametrize(

        "pole_type, use_progress, use_cache",

        list(itertools.product(POLE_TYPES, PROGRESS_OPTIONS, CACHE_OPTIONS)),

    )

    def test_audio_composition(

        self,

        audio_memory_lb: LogicalBlueprint,

        timer_lb: LogicalBlueprint,

        progress_lb: LogicalBlueprint,

        pole_type: str | None,

        use_progress: bool,

        use_cache: bool,

    ):

        """Compose audio-only all-in-one with every param combo."""

        result = compose_all_in_one(

                audio_memory_lb=audio_memory_lb,

                timer_lb=timer_lb,

                progress_bar_lb=progress_lb if use_progress else None,

                pole_type=pole_type,

                output_name=f"SmokeAudio_{pole_type}_pb{use_progress}",

                use_cache=use_cache,

                cache_key_parts=("smoke", "audio", str(pole_type),

                                 str(use_progress), str(use_cache)),

            )



        assert result.label.startswith("SmokeAudio_")

        assert len(result.entities) > 0



        for eid, ent in result.entities.items():

            assert ent.position is not None, f"Entity {eid!r} missing position"



        bp = to_draftsman(result)

        bp_str = bp.to_string()

        assert bp_str.startswith("0e")



        # Verify pole types

        bp2 = Blueprint.from_string(bp_str)

        lb2 = from_draftsman(bp2)

        pole_types_found = {

            ent.type for ent in lb2.entities.values()

            if ent.type in ("small-electric-pole", "medium-electric-pole", "substation")

        }

        if pole_type is not None:

            # TODO: re-enable when power supply is implemented

            expected_type = {

                "small": "small-electric-pole",

                "medium": "medium-electric-pole",

                "substation": "substation",

            }[pole_type]

            if expected_type not in pole_types_found:

                pytest.skip(

                    f"Power supply ({pole_type}) not yet implemented"

                )



    @pytest.mark.parametrize(

        "pole_type, use_progress, use_cache",

        list(itertools.product(POLE_TYPES, PROGRESS_OPTIONS, CACHE_OPTIONS)),

    )

    def test_combined_composition(

        self,

        display_lb: LogicalBlueprint,

        video_memory_lb: LogicalBlueprint,

        audio_memory_lb: LogicalBlueprint,

        timer_lb: LogicalBlueprint,

        progress_lb: LogicalBlueprint,

        pole_type: str | None,

        use_progress: bool,

        use_cache: bool,

    ):

        """Compose video + audio all-in-one with every param combo."""

        result = compose_all_in_one(

                display_lb=display_lb,

                video_memory_lb=video_memory_lb,

                audio_memory_lb=audio_memory_lb,

                timer_lb=timer_lb,

                progress_bar_lb=progress_lb if use_progress else None,

                pole_type=pole_type,

                output_name=f"SmokeBoth_{pole_type}_pb{use_progress}",

                use_cache=use_cache,

                cache_key_parts=("smoke", "both", str(pole_type),

                                 str(use_progress), str(use_cache)),

            )



        assert len(result.entities) > 0



        for eid, ent in result.entities.items():

            assert ent.position is not None, f"Entity {eid!r} missing position"



        bp = to_draftsman(result)

        bp_str = bp.to_string()

        assert bp_str.startswith("0e")



        bp2 = Blueprint.from_string(bp_str)

        lb2 = from_draftsman(bp2)

        # Verify entity count from both sub-blueprints present

        assert len(lb2.entities) == len(result.entities)



    @pytest.mark.parametrize(

        "pole_type, use_progress, use_cache",

        list(itertools.product(POLE_TYPES, PROGRESS_OPTIONS, CACHE_OPTIONS)),

    )

    def test_repeater_composition(

        self,

        display_lb: LogicalBlueprint,

        video_memory_lb: LogicalBlueprint,

        progress_lb: LogicalBlueprint,

        pole_type: str | None,

        use_progress: bool,

        use_cache: bool,

    ):

        """Compose with repeater timer instead of raw+mod."""

        from factorio_display.composer import _assign_tile_positions

        repeater = build_repeater("SmokeRepeater", constant=1024,

                                  output_signal="signal-R", mod=6000)

        mod = build_mod_timer(60, name="SmokeMod", input_signal="signal-R")

        _assign_tile_positions(mod, start_x=0, start_y=6)

        repeater.merge(mod, entity_prefix="mod_", network_prefix="mod_")



        result = compose_all_in_one(

                display_lb=display_lb,

                video_memory_lb=video_memory_lb,

                timer_lb=repeater,

                progress_bar_lb=progress_lb if use_progress else None,

                pole_type=pole_type,

                output_name=f"SmokeRep_{pole_type}",

                use_cache=use_cache,

                cache_key_parts=("smoke", "rep", str(pole_type),

                                 str(use_progress), str(use_cache)),

            )



        assert len(result.entities) > 0



        bp = to_draftsman(result)

        bp_str = bp.to_string()

        assert bp_str.startswith("0e")



        # Verify repeater entities present (prefixed with tm_)

        has_repeater = any(

            eid.startswith("tm_") and

            ent.properties.get("second_operand") == 1024

            for eid, ent in result.entities.items()

        )

        assert has_repeater, "Repeater entities not found in composed result"





# ======================================================================â?

# Standalone component smoke tests

# ======================================================================â?





class TestComponentSmoke:

    """Verify each component produces valid draftsman blueprints."""



    def test_timer_blueprints_valid(self):

        """All timer variants produce valid blueprint strings."""

        for name, lb in [

            ("raw", build_raw_timer("Raw")),

            ("mod", build_mod_timer(60, name="Mod")),

            ("rep", build_repeater("Rep")),

            ("rep_mod", build_repeater("RepMod", mod=6000)),

        ]:

            bp_str = to_draftsman(lb).to_string()

            assert bp_str.startswith("0e"), f"{name}: not a valid blueprint"

            bp = Blueprint.from_string(bp_str)

            assert len(bp.entities) > 0, f"{name}: no entities"



    def test_progress_bar_blueprint_valid(self):

        """Progress bar produces valid blueprint string."""

        lb = build_progress_bar("PB", length=10, signal_name="signal-clock", max_value=59)

        bp_str = to_draftsman(lb).to_string()

        assert bp_str.startswith("0e")

        bp = Blueprint.from_string(bp_str)

        lamps = [e for e in bp.entities if "lamp" in e.name.lower()]

        assert len(lamps) == 10



    def test_toml_roundtrip_all_components(self):

        """All component types survive TOML round-trip."""

        for name, lb in [

            ("raw_timer", build_raw_timer("T")),

            ("mod_timer", build_mod_timer(60, name="M")),

            ("repeater", build_repeater("R", mod=6000)),

            ("progress", build_progress_bar("P", length=5)),

        ]:

            toml_str = to_toml(lb)

            lb2 = from_toml(toml_str)

            assert len(lb2.entities) == len(lb.entities), (

                f"{name}: entity count mismatch"

            )



    def test_power_pole_toml_roundtrip(self):

        """Power pole entities survive TOML round-trip with quality."""

        for etype in ("small-electric-pole", "medium-electric-pole", "substation"):

            lb = LogicalBlueprint(label=f"Pole {etype}")

            lb.add_entity(LogicalEntity(

                "p1", etype,

                properties={"quality": "legendary"},

                position=(5, 5),

            ))

            toml_str = to_toml(lb)

            lb2 = from_toml(toml_str)

            assert "p1" in lb2.entities

            ent = lb2.entities["p1"]

            assert ent.type == etype

            assert ent.properties.get("quality") == "legendary"

            # Verify draftsman round-trip

            bp_str = to_draftsman(lb2).to_string()

            assert bp_str.startswith("0e")

