# MIDI Translator Evaluation — Summary

## Core fidelity: MIDI -> Factorio (rail_mode=auto, map_drums=off)

| scenario | rails | coverage | segmentation | rhythm | exact | onset | dur | vel | chroma_cos* | spec_cos* | env_corr* |
|---|---|---|---|---|---|---|---|---|---|---|---|
| melody | piano | 1.000 | 0.667 | 1.000 | 1.000 | 0.000 | 15.000 | 0.000 | 0.864 | 0.864 | 0.762 |
| wide_range | piano,bass,celesta | 1.000 | 0.733 | 1.000 | 0.909 | 0.000 | 0.000 | 0.742 | 0.744 | 0.640 | 0.637 |
| polyphonic | piano,bass | 1.000 | 0.617 | 1.000 | 0.966 | 0.000 | 0.000 | 1.000 | 0.748 | 0.675 | 0.887 |
| multitrack | piano,bass,drum,steel-drum | 0.814 | 0.349 | 1.000 | 0.867 | 0.000 | 0.000 | 0.831 | 0.741 | 0.602 | 0.819 |
| drums_only | drum | 0.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.141 | 0.025 | 0.855 |

* = timbre-free 'content' audio comparison (original vs reconstructed notes, same piano synth)


### Factorio in-game emulation (original vs speaker render)

| scenario | chroma_cos | dtw | env_corr | spec_cos |
|---|---|---|---|---|
| melody | 0.845 | 0.075 | 0.501 | 0.721 |
| wide_range | 0.754 | 0.122 | 0.477 | 0.551 |
| polyphonic | 0.765 | 0.116 | 0.473 | 0.573 |
| multitrack | 0.747 | 0.123 | 0.538 | 0.487 |
| drums_only | 0.162 | 0.419 | 0.992 | 0.089 |


## Effect of `--map-drums` (below-range notes -> kick drum)

| scenario | rails | coverage | exact | note_count(ref/rec) |
|---|---|---|---|---|
| melody | piano | 1.000 | 1.000 | 15/10 |
| wide_range | piano,drum,celesta | 0.667 | 1.000 | 15/15 |
| polyphonic | piano,drum | 0.979 | 1.000 | 47/36 |
| multitrack | piano,drum,steel-drum | 0.488 | 0.929 | 43/38 |
| drums_only | drum | 0.000 | 0.000 | 16/16 |


## AI transcription (Basic Pitch) end-to-end

| chain | stage | coverage | seg | rhythm | exact | onset | precision | chroma_cos | env_corr | spec_cos |
|---|---|---|---|---|---|---|---|---|---|---|
| melody | AI transcribe | 1.000 | 1.000 | 1.000 | 0.267 | 0.818 | 0.500 | 0.856 | 0.495 | 0.804 |
| melody | +Factorio | 1.000 | 0.733 | 1.000 | 0.091 | 1.000 | 0.524 | - | - | - |
| polyphonic | AI transcribe | 1.000 | 1.000 | 1.000 | 0.532 | 0.273 | 0.505 | 0.783 | 0.535 | 0.617 |
| polyphonic | +Factorio | 1.000 | 0.787 | 1.000 | 0.378 | 1.000 | 0.552 | - | - | - |
| stft_melody | AI transcribe | 1.000 | 0.667 | 1.000 | 0.100 | 3.000 | 0.039 | 0.834 | 0.344 | 0.793 |
| stft_melody | +Factorio | 1.000 | 0.533 | 1.000 | 0.500 | 3.000 | 0.032 | - | - | - |
| stft_polyphonic | AI transcribe | 1.000 | 0.957 | 1.000 | 0.022 | 2.000 | 0.091 | 0.808 | 0.381 | 0.618 |
| stft_polyphonic | +Factorio | 1.000 | 0.851 | 1.000 | 0.075 | 2.000 | 0.089 | - | - | - |
