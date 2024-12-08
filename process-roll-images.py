#!/usr/bin/env python3

"""
Produces raw, note, and (if desired) expressionized MIDI files (written to
subfolders of midi/), as well as a hole analysis output file (written to the
txt/ folder) for each specified piano roll. Rolls to be processed can be
specified by DRUID on the command line (separated by spaces) or in a plain-text
file (one DRUID per line) or a CSV file (DRUIDs in the "Druid" column). For
each roll, the script downloads the roll's IIIF manifest (JSON format) and an
image of the rolls can from the Stanford Digital Repository (if these are not
already cached in subfolders). Image file formats may vary by generation of the
scans. Advanced features are also available to process locally downloaded image
files if they have not yet been accessioned into the Stanford Digital Repository.
The script uses the tiff2holes executable from
https://github.com/pianoroll/roll-image-parser to perform the image hole-
parsing analysis, then extracts the binasc-encoded raw and note MIDI data from
the analysis file.
The external binasc tool (https://github.com/craigsapp/binasc) is used to
convert the extracted MIDI data to binary MIDI files. Then, if desired, the
midi2exp executable from https://github.com/pianoroll/midi2exp/ generates
expressionized MIDI files.
"""

import argparse
from csv import DictReader
import json
import logging
import os
from pathlib import Path
import re
from shutil import copyfileobj

from lxml import etree
from openjpeg import decode  # Necessary to read JPEG2000s
from PIL import Image
import requests

# Otherwise Pillow will refuse to open large images
Image.MAX_IMAGE_PIXELS = None

# As new types are added to the collection, their shorthands must be added here
ROLL_TYPES = [
    "welte-red",
    "88-note",
    "65-note",
    "welte-green",
    "welte-licensee",
    "duo-art",
]

ROLL_TYPE_ENTRIES = {
    "Welte-Mignon red roll (T-100)": "welte-red",
    "Welte-Mignon red roll (T-100).": "welte-red",
    "Welte-Mignon red roll (T-100)..": "welte-red",
    "Scale: 88n": "88-note",
    "Scale: 88n.": "88-note",
    "Scale: 65n.": "65-note",
    "88n": "88-note",
    "65n": "65-note",
    "standard": "88-note",
    "non-reproducing": "88-note",
    "Welte-Mignon green roll (T-98)": "welte-green",
    "Welte-Mignon green roll (T-98).": "welte-green",
    "Welte-Mignon licensee roll": "welte-licensee",
    "Welte-Mignon licensee roll.": "welte-licensee",
    "Welte-Mignon licensee roll (T-98).": "welte-licensee",
    "Duo-Art piano rolls": "duo-art",
    "Duo-Art piano rolls.": "duo-art",
}

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
    "kz379jn2491",
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
    "qw257qp8232",
]

# This can be used in a last-ditch attempt to strongarm a roll's tracker
# alignment after all of the other methods has been run. Negative values
# shift the tracker assignments left, positives to the right.
MANUAL_ALIGNMENT_CORRECTIONS = {}

# These are either duplicates of existing rolls, or rolls that are listed in
# DRUIDs files but have disappeared from the catalog, or rolls that were
# accessioned incorrectly (hm136vg1420)
ROLLS_TO_SKIP = [
    "rr052wh1991",
    "zf037wk3650",
    "hm136vg1420",
    "df354sy6634",  # Needs to be flipped vertically
    "sm367hr9769",  # Image downloads but won't process automatically
    "xc735nd8093",  # Needs to be flipped vertically, reaccessioned
]

TIFF2HOLES = "../roll-image-parser/bin/tiff2holes"
BINASC = "../binasc/binasc"
MIDI2EXP = "../midi2exp/bin/midi2exp"

PURL_BASE = "https://purl.stanford.edu/"

NS = {"x": "http://www.loc.gov/mods/v3"}


def get_roll_type_for_druid(druid, redownload_xml):
    """Obtains a .xml metadata file for the roll specified by DRUID either
    from the local xml/ folder or the Stanford Digital Repository, then
    parses the XML to build the metadata dictionary for the roll.
    """

    def get_value_by_xpath(xpath):
        try:
            return xml_tree.xpath(
                xpath,
                namespaces=NS,
            )[0]
        except IndexError:
            return None

    xml_filepath = Path(f"xml/{druid}.xml")

    if not xml_filepath.exists() or redownload_xml:
        response = requests.get(f"{PURL_BASE}{druid}.xml")
        xml_data = response.text
        with xml_filepath.open("w", encoding="utf-8") as _fh:
            _fh.write(xml_data)
    else:
        xml_data = xml_filepath.open("r", encoding="utf-8").read()

    try:
        mods_xml = (
            "<mods" + xml_data.split(r"<mods")[1].split(r"</mods>")[0] + "</mods>"
        )
        xml_tree = etree.fromstring(mods_xml)
    except etree.XMLSyntaxError:
        logging.error(
            f"Unable to parse XML metadata for {druid} - record is likely missing."
        )
        return None

    # The representation of the roll type in the MODS metadata continues to
    # evolve. Hopefully this logic covers all cases.
    roll_type = "NA"
    type_note = get_value_by_xpath(
        "x:physicalDescription/x:note[@displayLabel='Roll type']/text()"
    )
    scale_note = get_value_by_xpath(
        "x:physicalDescription/x:note[@displayLabel='Scale']/text()"
    )
    if type_note is not None and type_note in ROLL_TYPE_ENTRIES:
        roll_type = ROLL_TYPE_ENTRIES[type_note]

    if (
        scale_note is not None
        and scale_note in ROLL_TYPE_ENTRIES
        and (roll_type == "NA" or type_note == "standard")
    ):
        roll_type = ROLL_TYPE_ENTRIES[scale_note]

    if roll_type == "NA" or type_note == "standard":
        for note in xml_tree.xpath("(x:note)", namespaces=NS):
            if (
                note is not None
                and note.text in ROLL_TYPE_ENTRIES
                # Most rolls of any type are marked as "88n", so don't let this
                # setting overwrite a more specific roll type note.
                and (ROLL_TYPE_ENTRIES[note.text] != "88-note" or roll_type == "NA")
            ):
                roll_type = ROLL_TYPE_ENTRIES[note.text]

    return roll_type


def get_iiif_manifest(druid, redownload_manifests=True):
    """If the IIIF manifest file (.json format) is not present in manifests/
    or the redownload_manifests parameters is set, downloads it from the
    Stanford Digital Repository. The manifest is then parsed into a
    dictionary."""

    iiif_filepath = Path(f"manifests/{druid}.json")
    if iiif_filepath.exists() and not redownload_manifests:
        iiif_manifest = json.load(open(iiif_filepath, "r"))
    else:
        try:
            response = requests.get(f"{PURL_BASE}{druid}/iiif/manifest")
            iiif_manifest = response.json()
            with iiif_filepath.open("w") as _fh:
                json.dump(iiif_manifest, _fh)
        except Exception as e:
            logging.info(f"Unable to download IIIF manifest for {druid}")
            iiif_manifest = None
    return iiif_manifest


def get_image_url(iiif_manifest):
    """Finds the download URL for the highest-resolution TIFF image of the roll
    that is listed in the IIIF manifest data (usually this is a monochrome,
    green-channel only TIFF image."""

    if iiif_manifest is None or (
        "sequences" not in iiif_manifest and "items" not in iiif_manifest
    ):
        logging.error("Couldn't find sequences in IIIF manifest")
        return None

    renderings = []
    if "sequences" in iiif_manifest:
        seqs = iiif_manifest["sequences"]
    else:
        seqs = iiif_manifest["items"]

    # Handle a variety of potential IIIF manifest formats for sequences/renderings/canvases
    for seq in seqs:
        if "renderings" in seq:
            renderings = seq["renderings"]
        elif "rendering" not in seq:
            if "canvases" not in seq:
                continue
            renderings = [canvas["rendering"][0] for canvas in seq["canvases"]]
        else:
            renderings = seq["rendering"]

        # If there's only one rendering (probably the original RGB), return it
        if len(renderings) == 1:
            if "id" in renderings[0]:
                return renderings[0]["id"]
            elif "@id" in renderings[0]:
                return renderings[0]["@id"]

        for rendering in renderings:
            if (
                rendering["@id"].endswith("_ir_sp.jp2")
                or rendering["@id"].endswith("_gs.jp2")
            ) and rendering["format"] == "image/jp2":
                return rendering["@id"]
            if (
                rendering["@id"].endswith("_gr.tiff")
                or rendering["@id"].endswith("_gr.tif")
            ) and (
                rendering["format"] == "image/tiff"
                or rendering["format"] == "image/x-tiff-big"
            ):
                return rendering["@id"]

    logging.error("Unable to find image URL in IIIF manifest")
    return None


def get_druids_from_csv_file(druids_fp):
    """Returns a list of the DRUIDs in the "Druid" column of the specified CSV
    file."""

    if not Path(druids_fp).exists():
        logging.error(f"Unable to find DRUIDs file {druids_fp}")
        return []
    druids_list = []
    with open(druids_fp, "r", newline="") as druid_csv:
        druid_reader = DictReader(druid_csv)
        for row in druid_reader:
            druids_list.append(row["Druid"])
    return druids_list


def get_druids_from_txt_file(druids_fp):
    """If the specified text input file contains one DRUID per line, parses it
    into a list of DRUIDs."""

    if not Path(druids_fp).exists():
        logging.error(f"Unable to find DRUIDs file {druids_fp}")
        return []
    druids_list = []
    with open(druids_fp, "r") as druid_txt:
        for line in druid_txt:
            druids_list.append(line.strip())
    return druids_list


def request_image(image_url):
    """Attempts to download the file at the URL specified and, if available,
    returns it as a raw response object."""

    if image_url is None:
        logging.error("Image URL is None")
        return None
    logging.info(f"Downloading roll image {image_url}")
    response = requests.get(image_url, stream=True)
    if response.status_code == 200:
        response.raw.decode_content = True
        return response

    logging.error(f"Unable to download {image_url} - {response}")
    return None


def get_roll_image(
    druid,
    image_url,
    roll_type,
    redownload_image=False,
    mirror_roll=False,
    gen2scan=False,
):
    """Attempts to download an image of the roll specified by DRUID if a URL is
    provided, otherwise searches for the roll in the local images/ folder.
    Applies left-right flipping (mirroring) logic if appropriate, and returns
    a path to the image file."""

    image_already_mirrored = False

    target_pathname = Path(f"images/{image_url.split('/')[-1]}")
    image_filepath = Path(f"images/{druid}.tiff")

    # If the source image is a JP2, prepare to convert it to a TIFF
    if image_url.endswith(".jp2"):
        source_filepath = Path(f"images/{druid}.jp2")
    else:
        source_filepath = target_pathname

    # If the source image is a TIFF and it's stored locally, stop here
    if (
        not redownload_image
        and os.path.isfile(target_pathname)
        and target_pathname.suffix in (".tiff", ".tif")
    ):
        return target_pathname

    # Otherwise, download the image and convert it to a TIFF if necessary
    if not os.path.isfile(image_filepath) or redownload_image:
        if target_pathname.suffix in (".tiff", ".tif"):
            source_filepath = target_pathname
            image_filepath = target_pathname

        if image_url.endswith(".jp2") and os.path.isfile(source_filepath):
            logging.info("JPEG2000 already downloaded")
        else:
            response = request_image(image_url)
            if response is not None:
                with open(source_filepath, "wb") as image_file:
                    copyfileobj(response.raw, image_file)
            del response
        # High-contrast infrared versions of Gen2 scans are JP2s, must be
        # converted in place into TIFFs and flipped vertically for parsing
        if image_url.endswith(".jp2"):
            logging.info(f"Converting JPEG2000 to TIFF: {source_filepath}")
            image_array = decode(source_filepath)
            img = Image.fromarray(image_array)
            # XXX Need to check all contingencies...
            if gen2scan or roll_type != "welte-red":
                logging.info(f"Flipping image top-bttom: {image_filepath}")
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            img.save(image_filepath)
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
    """Sometimes a downloaded roll image needs to be flipped left-right
    (mirrored), for reasons described elsewhere in this documentation.
    Performs this mirroring in place."""

    logging.info(f"Flipping image left-right: {image_filepath}")
    img = Image.open(image_filepath)
    out = img.transpose(Image.FLIP_LEFT_RIGHT)
    out.save(image_filepath)


def parse_roll_image(
    druid,
    image_filepath,
    roll_type,
    ignore_rewind_hole,
    tiff2holes,
    is_monochrome,
    gen2scan,
):
    """Runs the external tiff2holes roll image parsing tool on roll image for
    the DRUID specified in the parameters, adding the appropriate command-line
    switches and parameters to the command."""

    if not Path(tiff2holes).exists():
        logging.error(f"tiff2holes executable not found at {tiff2holes}")
        return
    if image_filepath is None or roll_type == "NA":
        logging.info(f"No image at {image_filepath} or roll type unknown")
        return

    t2h_switches = ""

    # Gen2 scans _should_ all be multi-channel
    if is_monochrome and not gen2scan:
        t2h_switches = "-m "

    if roll_type == "welte-red":
        t2h_switches += "-r"
    elif roll_type == "88-note":
        t2h_switches += "-8"
    elif roll_type == "65-note":
        t2h_switches += "-5"
    elif roll_type == "welte-green":
        t2h_switches += "-g"
    elif roll_type == "welte-licensee":
        t2h_switches += "-l"
    elif roll_type == "duo-art":
        t2h_switches += "-d"

    if ignore_rewind_hole:
        t2h_switches += " -s"

    if druid in MANUAL_ALIGNMENT_CORRECTIONS:
        t2h_switches += f" --alignment-shift={MANUAL_ALIGNMENT_CORRECTIONS[druid]}"

    cmd = f"{tiff2holes} {t2h_switches} {image_filepath} > txt/{druid}.txt 2> logs/{druid}.err"
    logging.info(f"Running image parser on {image_filepath} (roll type {roll_type})")
    os.system(cmd)


def convert_binasc_to_midi(binasc_data, druid, midi_type, binasc):
    """Invokes the external binasc tool to convert the provided ASCII-encoded
    hexadecimal representation of a MIDI file to binary MIDI format. This
    involves first writing the input data to a separate .binasc file and then
    running the binasc tool on it to generate a .mid file."""

    if not Path(binasc).exists():
        logging.error(f"binasc executable not found at {binasc}")
        return
    binasc_file_path = f"binasc/{druid}_{midi_type}.binasc"
    with open(binasc_file_path, "w") as binasc_file:
        binasc_file.write(binasc_data)
    if Path(binasc).exists():
        cmd = f"{binasc} {binasc_file_path} -c midi/{midi_type}/{druid}_{midi_type}.mid"
        os.system(cmd)


def extract_midi_from_analysis(druid, regenerate_midi, binasc):
    """Extracts the ASCII-encoded hexadecmial representations of a roll's raw
    and note MIDI realization from the .txt output data file produced via
    the tiff2holes roll image parsing tool (see parse_roll_image()). Via
    convert_binasc_to_midi(), these realizations are written to local files as
    DRUID_note.mid and DRUID_raw.mid if they are not already present or the
    regenerate_midi parameter is true."""

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
        convert_binasc_to_midi(holes_data, druid, "raw", binasc)
        notes_data = (
            re.search(r"^@MIDIFILE:$(.*)", contents, re.M | re.S)
            .group(1)
            .split("\n@")[0]
        )
        convert_binasc_to_midi(notes_data, druid, "note", binasc)


def apply_midi_expressions(druid, roll_type, midi2exp):
    """Invokes the external midi2exp tool to create an expressive version of
    the note MIDI realization of a roll image, adding the appropriate
    command-line parameters to the command to run the tool."""

    if not Path(midi2exp).exists():
        logging.error(f"midi2exp executable not found at {midi2exp}")
        return
    if not Path(f"midi/note/{druid}_note.mid").exists():
        logging.error(
            f"Note MIDI file does not exist at midi/note/{druid}_note.mid, cannot apply expressions"
        )
        return
    if roll_type == "65-note":
        return

    # The -r switch removes the control tracks (3-4, 0-indexed)
    m2e_switches = (
        "-r -adjust-hole-lengths"  # add --ac 0 for no acceleration, when available
    )
    if roll_type == "welte-red":
        m2e_switches += " -w"
    elif roll_type == "welte-green":
        m2e_switches += " -g"
    elif roll_type == "welte-licensee":
        m2e_switches += " -l"
    elif roll_type == "88-note":
        m2e_switches += " -h"
    elif roll_type == "duo-art":
        m2e_switches += " -u"
    cmd = (
        f"{midi2exp} {m2e_switches} midi/note/{druid}_note.mid midi/exp/{druid}_exp.mid"
    )
    logging.info(f"Running expression extraction on midi/note/{druid}_note.mid")
    os.system(cmd)
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
        "-t",
        "--roll_type",
        choices=ROLL_TYPES,
        default="NA",
        help=f"Type of the roll(s) ({' '.join(ROLL_TYPES)})",
    )
    argparser.add_argument(
        "-c",
        "--druids-csv-file",
        help="Path to a CSV file listing rolls, with DRUIDs in the 'Druid' column",
    )
    argparser.add_argument(
        "-f",
        "--druids-txt-file",
        help="Path to a plain text file listing DRUIDs to be processed, one per line",
    )
    argparser.add_argument(
        "--multichannel-tiffs",
        action="store_true",
        help="Set if the TIFF images to be processed are RGB, not monochrome",
    )
    argparser.add_argument(
        "--redownload-manifests",
        action="store_true",
        help="Always download IIIF manifests, overwriting files in manifests/",
    )
    argparser.add_argument(
        "--redownload-metadata",
        action="store_true",
        help="Always download XML metadata files, overwriting files in xml/",
    )
    argparser.add_argument(
        "--redownload-images",
        action="store_true",
        help="Always download roll images, overwriting files in images/",
    )
    argparser.add_argument(
        "--reprocess-images",
        action="store_true",
        help="Always parse roll images, overwriting output files in txt/",
    )
    argparser.add_argument(
        "--mirror-images",
        action="store_true",
        help="Mirror (flip left/right) all roll images being processed",
    )
    argparser.add_argument(
        "--ignore-rewind-hole",
        action="store_true",
        help="Ignore reported rewind hole position when assigning MIDI numbers to holes",
    )
    argparser.add_argument(
        "--regenerate-midi",
        action="store_true",
        help="Always generate new _raw.mid and _note.mid MIDI files, overwriting existing versions",
    )
    argparser.add_argument(
        "--no-expression",
        action="store_true",
        help="Do not apply expression emulation to create -exp.mid MIDI files (preexisting files will remain)",
    )
    argparser.add_argument(
        "--tiff2holes",
        default=TIFF2HOLES,
        help="Location of a compiled tiff2holes binary",
    )
    argparser.add_argument(
        "--binasc",
        default=BINASC,
        help="Location of a compiled binasc binary",
    )
    argparser.add_argument(
        "--midi2exp",
        default=MIDI2EXP,
        help="Location of a compiled midi2exp binary",
    )
    argparser.add_argument(
        "--gen2scan",
        action="store_true",
        help="Scan is from the updated camera (2024-)",
    )

    args = argparser.parse_args()

    # Adding DRUIDs here will override user input
    druids = []

    if len(args.druids) > 0:
        druids = args.druids
    elif args.druids_csv_file is not None:
        druids = get_druids_from_csv_file(args.druids_csv_file)
    elif args.druids_txt_file is not None:
        druids = get_druids_from_txt_file(args.druids_txt_file)

    for druid in druids:
        if druid in ROLLS_TO_SKIP:
            logging.info(f"Skippig DRUID {druid}")
            continue

        logging.info(f"Downloading and processing {druid}...")

        iiif_manifest = get_iiif_manifest(druid, args.redownload_manifests)

        if args.roll_type != "NA":
            roll_type = args.roll_type
        else:
            roll_type = get_roll_type_for_druid(druid, args.redownload_metadata)
            logging.info(f"Roll type for {druid} is {roll_type}")

        roll_image = get_roll_image(
            druid,
            get_image_url(iiif_manifest),
            roll_type,
            args.redownload_images,
            args.mirror_images,
            args.gen2scan,
        )

        if args.reprocess_images or (
            not Path(f"txt/{druid}.txt").exists() and roll_image is not None
        ):
            parse_roll_image(
                druid,
                roll_image,
                roll_type,
                args.ignore_rewind_hole or (druid in IGNORE_REWIND_HOLE),
                args.tiff2holes,
                not args.multichannel_tiffs,
                args.gen2scan,
            )

        extract_midi_from_analysis(druid, args.regenerate_midi, args.binasc)

        if not args.no_expression:
            apply_midi_expressions(druid, roll_type, args.midi2exp)


if __name__ == "__main__":
    main()
