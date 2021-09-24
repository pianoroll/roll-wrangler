# roll-wrangler

Python scripts to download and process roll images from the Stanford Digital Repository

## Installation

After creating a local copy of the repository

`git clone https://github.com/pianoroll/roll-wrangler.git`

the easiest way to ensure the scripts can be run is to make sure
[Pipenv](https://pypi.org/project/pipenv/) is installed on your system (for Mac
OS, it's best to use [Homebrew](https://formulae.brew.sh/formula/pipenv)). Then
run

`pipenv install`

from within the `roll-wrangler`/ folder to set up a Python
environment and install the necessary external Python modules.

## Example

This is a standard invocation of the `process-roll-images.py` script:

`pipenv run python process-roll-images.py hk155fw7898 --reprocess_images --regenerate_midi --tiff2holes_dir ../roll-image-parser/bin/ --binasc_dir ../binasc/ --midi2exp_dir ../midi2exp/bin/`

These command-line arguments direct the script to download and process a single
roll (hk155fw7898); the `--reprocess_images` flag indicates that the image
should be re-parsed even if an analysis output file already exists in `txt/`
(useful if, for example, an error occurred during the previous analysis). The
`--regenerate_midi` flag similarly indicates that any pre-existing MIDI output
files for the roll should be overwritten. The usage info for the script explains
all of the other available command-line options and can be accessed by running

`pipenv run python process-roll-images.py -h`

Note also the command-line arguments specifying the folders where the
`tiff2holes`, `binasc` and `midi2exp` executables can be found. These programs
can be compiled from the following Github repositories:

- `tiff2holes`: https://github.com/pianoroll/roll-image-parser
- `binasc`: https://github.com/craigsapp/binasc
- `midi2exp`: https://github.com/pianoroll/midi2exp

The downloaded roll images are stored in `images/` and the output MIDI files are
written to `midi/raw/DRUID_raw.mid` (containing one MIDI message per
perforation), `midi/note/DRUID_note.mid` (with continuations grouped into MIDI
notes) and `midi/exp/DRUID_exp.mid` (if desired; emulated expression is
encoded as MIDI note velocities, with pedal events and acceleration emulation
added).

The script also downloads a IIIF manifest file for each roll to determine the
image URL and to obtain other information necessary to parse the roll image,
such as the roll type; it is stored in `manifests/DRUID.json`. Other output
files written to sub-folders are `txt/DRUID.txt` (the analysis output of
`tiff2holes`), `logs/DRUID.err` (stderr output of `tiff2holes`), and
hex-encoded versions of the raw and note midi files, written to the `binasc/`
folder.
