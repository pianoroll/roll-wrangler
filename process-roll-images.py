#!/usr/bin/env python3

""" This script produces raw, note, and (if desired) expressionized MIDI    """
""" files (written to subfolders of midi/), as well as a hole analysis      """
""" output file (written to the txt/ folder) for each specified piano roll. """
""" Rolls to be processed can be specified by DRUID on the command line     """
""" (separated by spaces) or in a CSV file (DRUIDs in the "Druid" column).  """
""" For each roll, the script downloads the roll's MODS metadata file, its  """
""" IIIF manifest, and the roll image TIFF file from the SDR (if these are  """
""" not already cached in subfolders).                                      """
""" The script then uses the tiff2holes executable from                     """
""" https://github.com/pianoroll/roll-image-parser to perform the image/    """
""" hole-parsing analysis, then extracts the binasc-encoded raw and note    """
""" MIDI data from the analysis file.                                       """
""" The external binasc tool (https://github.com/craigsapp/binasc) is used  """
""" to convert the extracted MIDI data to binary MIDI files.                """
""" Then, if desired, the the midi2exp executable from                      """
""" https://github.com/pianoroll/midi2exp/ generates expressionized MIDI    """
""" files.                                                                  """

import argparse
from csv import DictReader
import json
import logging
from os import system
from pathlib import Path
import re
from shutil import copyfileobj

from PIL import Image
import requests

# Otherwise Pillow will refuse to open large images
Image.MAX_IMAGE_PIXELS = None

# As new types are added to the collection, their shorthands must be added here
ROLL_TYPES = ["welte-red", "88-note", "65-note"]

# The downloadable full-resolution monochome TIFF images of several rolls were
# erroneously mirrored left-right (so that the bass perforations are on the
# right side of the image). There may be others like this...
REVERSED_IMAGES = [
    "yt837kd6607",
    "ws749sk4778",
    "hs635sh6729",
    "zw485gh6070",
    "xr682fm1233",
    "mx460bt7026",
    "cs175wr2428",
    "bz327kz4744",
    "wv912mm2332",
    "jw822wm2644",
    "fv104hn7521",
    "fy803vj4057",
]

# This overrides the --ignore_rewind_hole command line switch; both are used
# to ignore the detected rewind hole position for a roll when assigning MIDI
# numbers to hole columns (tracker bar positions); the rewind hole position
# can be detected incorrectly due to test patterns at the end of the roll,
# conjoined rolls, or spurious holes, and sometimes it's better to ignore it
# and hope the other alignment methods will assign the MIDI numbers correctly.
IGNORE_REWIND_HOLE = [
    "mh156nr8259",
    "cd381jt9273",
]

# These are either duplicates of existing rolls, or rolls that are listed in
# DRUIDs files but have disappeared from the catalog.
ROLLS_TO_SKIP = ["rr052wh1991", "hm136vg1420"]

TIFF2HOLES_DIR = "../roll-image-parser/bin/"
BINASC_DIR = "../binasc/"
MIDI2EXP_DIR = "../midi2exp/bin/"

PURL_BASE = "https://purl.stanford.edu/"


def get_iiif_manifest(druid, redownload_manifests=True):
    iiif_filepath = Path(f"manifests/{druid}.json")
    if iiif_filepath.exists() and not redownload_manifests:
        iiif_manifest = json.load(open(iiif_filepath, "r"))
    else:
        response = requests.get(f"{PURL_BASE}{druid}/iiif/manifest")
        iiif_manifest = response.json()
        with iiif_filepath.open("w") as _fh:
            json.dump(iiif_manifest, _fh)
    return iiif_manifest


def get_tiff_url(iiif_manifest):
    if "rendering" not in iiif_manifest["sequences"][0]:
        return None

    for rendering in iiif_manifest["sequences"][0]["rendering"]:
        if (
            rendering["format"] == "image/tiff"
            or rendering["format"] == "image/x-tiff-big"
        ):
            return rendering["@id"]
    return None


def get_roll_type(iiif_manifest):
    roll_type = "NA"
    for [label, value] in iiif_manifest["metadata"]:
        if label == "Description" and "88n" in value:
            roll_type = "88-note"
        elif label == "Description" and "65n" in value:
            roll_type = "65-note"
        elif (
            label == "Description" and "Welte-Mignon red roll (T-100)" in value
        ):
            roll_type = "welte-red"
            # Welte roll metadata can also include "Scale: 88n", so stop as
            # soon as we see that it's a T-100 roll
            break
    return roll_type


def get_druids_from_file(druids_fp):
    if not Path(druids_fp).exists():
        logging.error(f"Unable to find DRUID CSV file {druids_fp}")
        return []
    druids_list = []
    with open(druids_fp, "r", newline="") as druid_csv:
        druid_reader = DictReader(druid_csv)
        for row in druid_reader:
            druids_list.append(row["Druid"])
    return druids_list


def request_image(image_url):
    logging.info(f"Downloading roll image {image_url}")
    response = requests.get(image_url, stream=True)
    if response.status_code == 200:
        response.raw.decode_content = True
        return response
    else:
        logging.error(f"Unable to download {image_url} - {response}")
        return None


def get_roll_image(druid, image_url, redownload_image=False, mirror_roll=False):
    image_already_mirrored = False
    image_fn = re.sub("\.tif$", ".tiff", image_url.split("/")[-1])
    image_filepath = Path(f"images/{image_fn}")
    if not image_filepath.exists() or redownload_image:
        response = request_image(image_url)
        with open(image_filepath, "wb") as image_file:
            copyfileobj(response.raw, image_file)
        del response
        # Always flip a roll's image on first download if it's known to be
        # improperly mirrored
        if druid in REVERSED_IMAGES:
            flip_image_left_right(image_filepath)
            image_already_mirrored = True
    # Don't re-flip the image after the first download, even if specified on
    # the cmd line -- this would just flip it back to its initial orientation
    if mirror_roll and not image_already_mirrored:
        flip_image_left_right(image_filepath)
    return image_filepath


def flip_image_left_right(image_filepath):
    logging.info(f"Flipping image left-right: {image_filepath}")
    im = Image.open(image_filepath)
    out = im.transpose(Image.FLIP_LEFT_RIGHT)
    out.save(image_filepath)


def parse_roll_image(
    druid, image_filepath, roll_type, ignore_rewind_hole, tiff2holes_dir
):
    if not Path(f"{tiff2holes_dir}/tiff2holes").exists():
        logging.error(f"tiff2holes executable not found in {tiff2holes_dir}")
        return
    if image_filepath is None or roll_type == "NA":
        logging.info("No image at {image_filepath} or roll type unknown")
        return

    if roll_type == "welte-red":
        t2h_switches = "-m -r"
    elif roll_type == "88-note":
        t2h_switches = "-m -8"
    elif roll_type == "65-note":
        t2h_switches = "-m -5"

    if ignore_rewind_hole:
        t2h_switches += " -s"

    cmd = f"{tiff2holes_dir}/tiff2holes {t2h_switches} {image_filepath} > txt/{druid}.txt 2> logs/{druid}.err"
    logging.info(
        f"Running image parser on {druid} {image_filepath} {roll_type}"
    )
    system(cmd)


def convert_binasc_to_midi(binasc_data, druid, midi_type, binasc_dir):
    if not Path(f"{binasc_dir}/binasc").exists():
        logging.error(f"binasc executable not found in {binasc_dir}")
        return
    binasc_file_path = f"binasc/{druid}_{midi_type}.binasc"
    with open(binasc_file_path, "w") as binasc_file:
        binasc_file.write(binasc_data)
    if Path(f"{binasc_dir}/binasc").exists():
        cmd = f"{binasc_dir}/binasc {binasc_file_path} -c midi/{midi_type}/{druid}_{midi_type}.mid"
        system(cmd)


def extract_midi_from_analysis(druid, regenerate_midi, binasc_dir):
    if not Path(f"txt/{druid}.txt").exists():
        logging.error(
            f"Hole analysis report does not exist at txt/{druid}.txt, cannot extract MIDI"
        )
        return
    if not regenerate_midi and Path(f"midi/note/{druid}_note.mid").exists():
        logging.info(
            f"MIDI files already exist for {druid} and regenerate not specified, skipping"
        )
        return

    logging.info(f"Extracting MIDI from txt/{druid}.txt")
    with open(f"txt/{druid}.txt", "r") as analysis:
        contents = analysis.read()
        # NOTE: the binasc utility *requires* a trailing blank line at the end
        # of the text input
        holes_data = (
            re.search(r"^@HOLE_MIDIFILE:$(.*)", contents, re.M | re.S)
            .group(1)
            .split("\n@")[0]
        )
        convert_binasc_to_midi(holes_data, druid, "raw", binasc_dir)
        notes_data = (
            re.search(r"^@MIDIFILE:$(.*)", contents, re.M | re.S)
            .group(1)
            .split("\n@")[0]
        )
        convert_binasc_to_midi(notes_data, druid, "note", binasc_dir)


def apply_midi_expressions(druid, roll_type, midi2exp_dir):
    if not Path(f"{midi2exp_dir}/midi2exp").exists():
        logging.error(f"midi2exp executable not found in {midi2exp_dir}")
        return
    if not Path(f"midi/note/{druid}_note.mid").exists():
        logging.error(
            f"Note MIDI file does not exist at midi/note/{druid}_note.mid, cannot apply expressions"
        )
        return

    # The -r switch removes the control tracks (3-4, 0-indexed)
    m2e_switches = ""
    if roll_type == "welte-red":
        m2e_switches = "-w -r -adjust-hole-lengths"  # add --ac 0 for no acceleration, when available
    cmd = f"{midi2exp_dir}/midi2exp {m2e_switches} midi/note/{druid}_note.mid midi/exp/{druid}_exp.mid"
    logging.info(f"Running expression extraction on midi/note/{druid}_note.mid")
    system(cmd)
    return True


def main():
    """Command-line entry-point."""

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    argparser = argparse.ArgumentParser(
        description="Download and process roll image(s) to produce MIDI files"
    )
    argparser.add_argument(
        "druids",
        nargs="*",
        help="DRUID(s) of one or more rolls to be processed, separated by spaces",
    )
    argparser.add_argument(
        "-f",
        "--druids_csv_file",
        help="Path to a CSV file listing rolls, with DRUIDs in the 'Druid' column",
    )
    argparser.add_argument(
        "--redownload_manifests",
        action="store_true",
        help="Always download IIIF manifests, overwriting files in manifests/",
    )
    argparser.add_argument(
        "--redownload_images",
        action="store_true",
        help="Always download roll images, overwriting files in images/",
    )
    argparser.add_argument(
        "--reprocess_images",
        action="store_true",
        help="Always parse roll images, overwriting output files in txt/",
    )
    argparser.add_argument(
        "--mirror_images",
        action="store_true",
        help="Mirror (flip left/right) all roll images being processed",
    )
    argparser.add_argument(
        "--ignore_rewind_hole",
        action="store_true",
        help="Ignore reported rewind hole position when assigning MIDI numbers to holes",
    )
    argparser.add_argument(
        "--regenerate_midi",
        action="store_true",
        help="Always generate new _raw.mid and _note.mid MIDI files, overwriting existing versions",
    )
    argparser.add_argument(
        "--no_expression",
        action="store_true",
        help="Do not apply expression emulation to create _exp.mid MIDI files (preexisting files will remain)",
    )
    argparser.add_argument(
        "--tiff2holes_dir",
        default=TIFF2HOLES_DIR,
        help="Folder containing a compiled tiff2holes binary",
    )
    argparser.add_argument(
        "--binasc_dir",
        default=BINASC_DIR,
        help="Folder containing a compiled binasc binary",
    )
    argparser.add_argument(
        "--midi2exp_dir",
        default=MIDI2EXP_DIR,
        help="Folder containing a compiled midi2exp binary",
    )

    args = argparser.parse_args()

    # Adding DRUIDs here will override user input
    DRUIDS = []

    if "druids" in args:
        DRUIDS = args.druids
    elif "druids_csv_file" in args:
        DRUIDS = get_druids_from_file(args.druids_file)

    for druid in DRUIDS:

        if druid in ROLLS_TO_SKIP:
            logging.info(f"Skippig DRUID {druid}")
            continue

        logging.info(f"Downloading and processing {druid}...")

        iiif_manifest = get_iiif_manifest(druid, args.redownload_manifests)
        roll_image = get_roll_image(
            druid,
            get_tiff_url(iiif_manifest),
            args.redownload_images,
            args.mirror_images,
        )

        roll_type = get_roll_type(iiif_manifest)
        logging.info(f"Roll type for {druid} is {roll_type}")

        if args.reprocess_images or not Path(f"txt/{druid}.txt").exists():
            parse_roll_image(
                druid,
                roll_image,
                roll_type,
                args.ignore_rewind_hole or (druid in IGNORE_REWIND_HOLE),
                args.tiff2holes_dir,
            )

        extract_midi_from_analysis(druid, args.regenerate_midi, args.binasc_dir)

        if not args.no_expression:
            apply_midi_expressions(druid, roll_type, args.midi2exp_dir)


if __name__ == "__main__":
    main()
