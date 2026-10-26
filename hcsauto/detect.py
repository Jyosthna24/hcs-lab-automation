"""Work out which microscope produced an image folder, and read its acquisition metadata.

Supported:
  Opera Phenix (Revvity)       Images/Index.xml (or Index.idx.xml) + r02c03f01p01-ch1sk10fk1fl1.tiff
  ImageXpress (Molecular Dev.) TimePoint_N/ folders (or a .HTD file) + Plate_B03_w1.TIF

Everything that can be read from the data (image size, bit depth, pixel size,
binning, objective, timepoints, channels, wells) is read rather than typed in,
and the source of each value is recorded so the run is auditable.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import tifffile

from .metaxpress import find_htd, read_htd
from .plateplan.wells import convert

PHENIX_RE = re.compile(r"^(r\d\dc\d\d)f(\d+)p(\d+)-ch(\d+)sk(\d+)fk\d+fl\d+\.tiff?$", re.I)
IX_RE = re.compile(r"_([A-P]\d{2})(?:_s\d+)?_w(\d)\.tif$", re.I)


@dataclass
class InstrumentInfo:
    instrument: str                      # "phenix" | "imagexpress"
    image_folder: str
    image_shape: tuple[int, int]
    bit_depth: int
    pixel_size_um: float | None = None
    binning: int | None = None
    objective: float | None = None
    timepoints: list[int] = field(default_factory=list)
    channels: list[int] = field(default_factory=list)
    wells: list[str] = field(default_factory=list)   # plate-map naming (B02) - wells with any image
    plate_format: int | None = None                  # 96 / 384, from acquisition metadata
    selected_wells: list[str] = field(default_factory=list)  # wells the microscope was set to acquire
    expected_timepoints: int | None = None
    wavelengths: list[str] = field(default_factory=list)
    metadata_file: str | None = None
    sources: dict = field(default_factory=dict)      # where each value came from

    def to_dict(self) -> dict:
        d = asdict(self)
        d["image_shape"] = list(self.image_shape)
        return d


def _first(paths):
    return next(iter(paths), None)


def _tiff_basics(path: Path) -> tuple[tuple[int, int], int, str]:
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        shape = tuple(int(s) for s in page.shape[-2:])
        bits = int(np.dtype(page.dtype).itemsize * 8)
        desc = page.description or ""
    return shape, bits, desc


def _xml_value(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}[^>]*>([^<]+)</{tag}>", text)
    return m.group(1).strip() if m else None


def _detect_phenix(folder: Path) -> InstrumentInfo | None:
    img_dir = folder / "Images" if (folder / "Images").is_dir() else folder
    files = [p for p in img_dir.iterdir() if PHENIX_RE.match(p.name)] if img_dir.is_dir() else []
    if not files:
        return None
    parsed = [PHENIX_RE.match(p.name).groups() for p in files]
    shape, bits, _ = _tiff_basics(files[0])
    info = InstrumentInfo(
        instrument="phenix", image_folder=str(img_dir), image_shape=shape, bit_depth=bits,
        timepoints=sorted({int(g[4]) for g in parsed}),
        channels=sorted({int(g[3]) for g in parsed}),
        wells=sorted({convert(g[0], "imagexpress") for g in parsed}),
        sources={"image_shape": "tiff header", "bit_depth": "tiff header",
                 "timepoints": "file names", "channels": "file names", "wells": "file names"},
    )
    index = _first(p for p in (img_dir / "Index.xml", img_dir / "Index.idx.xml") if p.exists())
    if index is not None:
        xml = index.read_text(encoding="utf-8", errors="replace")
        res = _xml_value(xml, "ImageResolutionX")       # metres per pixel
        if res:
            info.pixel_size_um = float(res) * 1e6
            info.sources["pixel_size_um"] = index.name
        b = _xml_value(xml, "BinningX")
        if b:
            info.binning = int(float(b))
            info.sources["binning"] = index.name
        obj = _xml_value(xml, "ObjectiveMagnification")
        if obj:
            info.objective = float(obj)
            info.sources["objective"] = index.name
    return info


def _detect_imagexpress(folder: Path) -> InstrumentInfo | None:
    tp_dirs = sorted((d for d in folder.iterdir() if d.is_dir() and re.fullmatch(r"TimePoint_\d+", d.name)),
                     key=lambda d: int(d.name.split("_")[1]))
    search = tp_dirs or [folder]
    files = [p for d in search for p in d.iterdir() if IX_RE.search(p.name)]
    has_htd = any(folder.glob("*.HTD")) or any(folder.glob("*.htd"))
    if not files or not (tp_dirs or has_htd):
        return None
    shape, bits, desc = _tiff_basics(files[0])
    info = InstrumentInfo(
        instrument="imagexpress", image_folder=str(folder), image_shape=shape, bit_depth=bits,
        timepoints=[int(d.name.split("_")[1]) for d in tp_dirs] or [1],
        channels=sorted({int(IX_RE.search(p.name).group(2)) for p in files}),
        wells=sorted({IX_RE.search(p.name).group(1).upper() for p in files}),
        sources={"image_shape": "tiff header", "bit_depth": "tiff header",
                 "timepoints": "TimePoint_N folders", "channels": "file names", "wells": "file names"},
    )
    # MetaMorph writes acquisition settings as XML in the TIFF description
    m = re.search(r'id="spatial-calibration-x"[^>]*value="([\d.eE+-]+)"', desc)
    if m:
        info.pixel_size_um = float(m.group(1))
        info.sources["pixel_size_um"] = "tiff MetaMorph metadata"
    m = re.search(r'id="binning-x"[^>]*value="(\d+)"', desc)
    if m:
        info.binning = int(m.group(1))
        info.sources["binning"] = "tiff MetaMorph metadata"
    m = re.search(r'id="_MagSetting_"[^>]*value="(\d+)', desc)
    if m:
        info.objective = float(m.group(1))
        info.sources["objective"] = "tiff MetaMorph metadata"

    htd = find_htd(folder)
    if htd is not None:
        h = read_htd(htd)
        info.metadata_file = str(htd)
        info.plate_format = h.plate_format
        info.selected_wells = h.selected_wells
        info.expected_timepoints = h.timepoints
        info.wavelengths = h.wavelengths
        for k in ("plate_format", "selected_wells", "expected_timepoints", "wavelengths"):
            info.sources[k] = f"MetaXpress {htd.name}"
    return info


def detect_instrument(folder: Path | str) -> InstrumentInfo:
    """Identify the instrument from folder layout and file naming, then read metadata."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    for detector in (_detect_phenix, _detect_imagexpress):
        info = detector(folder)
        if info is not None:
            return info
    raise ValueError(f"{folder}: not recognised as an Opera Phenix or ImageXpress image folder")
