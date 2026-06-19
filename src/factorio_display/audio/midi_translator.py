import mido
import statistics
import argparse
import os

# --- 1. FACTORIO INSTRUMENT DEFINITIONS ---
FACTORIO_INSTRUMENTS = {
    'piano': {'min': 53, 'max': 100},          # F3-E7
    'bass': {'min': 41, 'max': 76},            # F2-E5
    'celesta': {'min': 77, 'max': 112},        # F5-E8
    'plucked': {'min': 65, 'max': 100},        # F4-E7
    'drum': {'min': 53, 'max': 88}             # F3-E6
}

# --- 2. AUTOMATED INSTRUMENT ROUTING ---
def map_gm_to_factorio(program, channel):
    if channel == 9: return 'drum'
    if 0 <= program <= 7: return 'piano'
    if 8 <= program <= 15: return 'celesta'
    if 24 <= program <= 31: return 'plucked'
    if 32 <= program <= 39: return 'bass'
    if 80 <= program <= 87: return 'bass'
    return 'piano'

# --- 3. CONTEXT-AWARE OCTAVE FOLDING ---
def fold_octaves(track_notes, target_instrument):
    if not track_notes: return []
    
    target_range = FACTORIO_INSTRUMENTS[target_instrument]
    target_center = (target_range['min'] + target_range['max']) // 2
    
    pitches = [msg.note for msg in track_notes if msg.type == 'note_on' and msg.velocity > 0]
    if not pitches: return track_notes
    median_pitch = statistics.median(pitches)
    
    shift_amount = target_center - median_pitch
    octave_shift = round(shift_amount / 12) * 12 
    
    folded_notes = []
    for msg in track_notes:
        if msg.type in ('note_on', 'note_off'):
            new_note = msg.note + octave_shift
            while new_note < target_range['min']: new_note += 12
            while new_note > target_range['max']: new_note -= 12
            folded_notes.append(msg.copy(note=int(new_note)))
        else:
            folded_notes.append(msg)
            
    return folded_notes

# --- 4. UNIFIED TIMING & DYNAMICS ENGINE ---
def process_timing(mid, min_note_gap_sec=0.06, chord_tolerance_sec=0.01, preview_mode=False, boost_melody=False):
    new_mid = mido.MidiFile()
    new_mid.ticks_per_beat = mid.ticks_per_beat
    
    # Pre-scan: Find the track with the highest average pitch to act as the "Melody"
    melody_track_idx = -1
    if boost_melody:
        highest_avg_pitch = -1
        for i, track in enumerate(mid.tracks):
            pitches = [msg.note for msg in track if msg.type == 'note_on' and msg.velocity > 0]
            if pitches:
                avg = sum(pitches) / len(pitches)
                if avg > highest_avg_pitch:
                    highest_avg_pitch = avg
                    melody_track_idx = i

    for i, track in enumerate(mid.tracks):
        is_melody_track = (i == melody_track_idx)
        absolute_events = []
        
        current_time_sec = 0.0
        absolute_tick = 0
        last_note_on_time = -1.0
        current_tempo = mido.bpm2tempo(120) 
        dropped_notes = set() # Track orphaned note_offs for Clean mode
        
        # --- PASS 1: Map to Absolute Timeline ---
        for msg in track:
            scaled_time = int(msg.time)
            absolute_tick += scaled_time
            
            if msg.type == 'set_tempo':
                current_tempo = msg.tempo
                new_msg = msg.copy(tempo=int(msg.tempo), time=0)
                absolute_events.append({'tick': absolute_tick, 'msg': new_msg, 'order': 0})
                continue
                
            is_note_off = msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0)
            
            if is_note_off:
                if preview_mode:
                    continue # Discard original note_offs in preview mode
                else:
                    if msg.note in dropped_notes:
                        dropped_notes.remove(msg.note)
                        continue
                    new_msg = msg.copy(time=0)
                    absolute_events.append({'tick': absolute_tick, 'msg': new_msg, 'order': 2})
                    continue
                    
            if msg.type == 'note_on' and msg.velocity > 0:
                time_in_sec = mido.tick2second(absolute_tick, mid.ticks_per_beat, current_tempo)
                time_since_last = time_in_sec - last_note_on_time
                
                is_chord = time_since_last <= chord_tolerance_sec
                
                # Prune notes that violate the gap (but preserve chords)
                if not is_chord and time_since_last < min_note_gap_sec:
                    if not preview_mode:
                        dropped_notes.add(msg.note)
                    continue 
                    
                last_note_on_time = time_in_sec
                
                # Dynamic Velocity Scaling & Melody Boost
                base_velocity = msg.velocity
                if is_melody_track:
                    base_velocity = min(127, int(base_velocity * 1.5)) # 50% Volume Boost
                    
                scaled_velocity = int((base_velocity / 127.0) * 100) # Map to 0-100
                msg = msg.copy(velocity=max(1, scaled_velocity), time=0)
                
                absolute_events.append({'tick': absolute_tick, 'msg': msg, 'order': 1})
                
                # Artificial Factorio Note Lengths for Preview Mode
                if preview_mode:
                    duration_ticks = int(((factorio_duration_sec * 1_000_000) / current_tempo) * mid.ticks_per_beat)
                    note_off_msg = mido.Message('note_off', note=msg.note, velocity=0, channel=msg.channel, time=0)
                    absolute_events.append({'tick': absolute_tick + duration_ticks, 'msg': note_off_msg, 'order': 2})
            else:
                new_msg = msg.copy(time=0)
                absolute_events.append({'tick': absolute_tick, 'msg': new_msg, 'order': 0})
                
        # --- PASS 2: Sort and Rebuild Delta Time ---
        absolute_events.sort(key=lambda e: (e['tick'], e['order']))
        
        new_track = mido.MidiTrack()
        new_mid.tracks.append(new_track)
        
        prev_tick = 0
        for event in absolute_events:
            msg = event['msg']
            delta_tick = event['tick'] - prev_tick
            msg.time = max(0, delta_tick)
            new_track.append(msg)
            prev_tick = event['tick']
            
    return new_mid

# --- MAIN PIPELINE ---
def translate_to_factorio(input_file, output_file, preview=False, boost=False):
    print(f"Loading {input_file}...")
    mid = mido.MidiFile(input_file)
    
    print(f"Processing Timing... (Preview: {preview}, Boost: {boost})")
    # Using slow_factor=1.0 for original speed. Change if it's too fast for polyphony.
    mid = process_timing(mid, preview_mode=preview, boost_melody=boost) 
    
    processed_mid = mido.MidiFile()
    processed_mid.ticks_per_beat = mid.ticks_per_beat
    
    for track in mid.tracks:
        new_track = mido.MidiTrack()
        processed_mid.tracks.append(new_track)
        
        current_instrument = 'piano' 
        for msg in track:
            if msg.type == 'program_change':
                current_instrument = map_gm_to_factorio(msg.program, msg.channel)
            elif hasattr(msg, 'channel') and msg.channel == 9:
                current_instrument = 'drum'
                
        folded_track = fold_octaves(track, current_instrument)
        for msg in folded_track:
            new_track.append(msg)
            
    processed_mid.save(output_file)
    print(f"Translation complete. Saved to {output_file}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert MIDI for Factorio Blueprinting.")
    parser.add_argument("input", help="Path to the input MIDI file")
    parser.add_argument("output", help="Path to save the output MIDI file")
    parser.add_argument("--preview", action="store_true", help="Simulate Factorio's 0.5s audio overlap")
    parser.add_argument("--boost-melody", action="store_true", help="Auto-detect highest track and boost volume by 1.5x")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"Error: Could not find file '{args.input}'")
    else:
        translate_to_factorio(args.input, args.output, preview=args.preview, boost=args.boost_melody)