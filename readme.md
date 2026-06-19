# Factorio Displayer

A command-line tool designed to encode media (videos, gifs, images, and audio tracks) into Factorio memory blueprints, allowing you to build massive animated screens and programmable speakers directly in-game. Designed with Factorio 2.0 and Draftsman.

## Usage

Factorio Displayer runs via a CLI command that generates raw blueprint strings, which you can pipe straight into your clipboard (e.g., `| Set-Clipboard` on Windows or `| pbcopy` on macOS). 

### 1. Export the Physical Display Grid
Generates the base blueprint of your physical video display containing wired lamps.
```bash
factorio-display export-display --width 28 --height 28
```

### 2. Export the Audio Decoder

Generates the blueprint unit used to demultiplex and interpret the memory audio signal for programmable speakers.

```bash
factorio-display export-audio --instrument bell
```

### 3. Encode Media

Converts a video file, gif, or image sequence into Decider Combinator "Memory" arrays.

```bash
factorio-display encode ./bad_apple.mp4 --name "Bad Apple Frame Data" --adaptive --fps 30
```

_Tip: Use --adaptive and --deduplicate to save massive amounts of combinators by skipping idle frames and recycling identical frames._

## How It Works in Factorio

This project maps complex arrays of binary data (pixels, sound pitches, and timing) into Factorio's circuit network by exploiting the new mechanics introduced in Factorio 2.0 Space Age.

### Video Display Implementation

In Factorio, a color display must process an X/Y grid of RGB pixels.

1.  **Signal Multiplexing:** We don't have enough colored wires to assign one per pixel. Instead, each physical pixel on the display is statically assigned a _unique Factorio signal identity_ (e.g., "iron-plate" at "legendary" quality). This mapping is precomputed and distributed.
    
2.  **The Display Screen:** Thousands of small lamps form the screen. Every single lamp is permanently wired to the same network but is configured to only read the specific signal/quality pair assigned to its coordinates.
    
3.  **The Memory Banks:** We use an array of Decider Combinators acting as Read-Only Memory (ROM). Each combinator represents a single frame of video. The condition of the Decider Combinator checks if the global signal-clock matches its designated frame number.
    
4.  **Playback:** A global clock counts upward (+1 per tick). When the clock matches a frame's assigned time index, that specific Decider Combinator activates and dumps a massive payload of signals onto the red wire. The lamps instantly decode their specific signals from this payload and light up with the correct RGB value, effectively refreshing the screen 60 times a second.
    

### Audio Player Implementation

Because Factorio's Programmable Speakers require explicit pitch mapping (0-36) and specific instrument signals, passing an audio sequence over time requires a filtering layer so signals don't overlap into a cacophony.

1.  **Mapping:** The system uses the available signal pool to map distinct signals to integer values (which correspond to specific pitches or ticks).
    
2.  **The Constant Combinator (Dictionary):** A single constant combinator holds the index definition, assigning signals a sequence of continuous integers (1, 2, 3...).
    
3.  **The Decider Combinator (Time Gate):** It compares the incoming signals against the global signal-clock tick. It filters the dictionary stream down to _only_ the signal assigned to the current tick.
    
4.  **First Arithmetic Combinator (Normalization):** It divides the output of the Decider Combinator by itself (Each / Each), transforming the output of the isolated tick signal into exactly 1.
    
5.  **Second Arithmetic Combinator (Target Application):** It multiplies the Normalized output (1 of the isolated signal) by the incoming Audio Memory Bank's payload (Each \* Each). This perfectly filters the sound memory down to exactly the pitch and volume intended for the current tick, and finally outputs it on the target sound signal (e.g., bell, lightning, programmable-speaker-instrument-piano), cleanly driving your speaker array.
