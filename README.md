# hcs-lab-automation

![tests](https://github.com/Jyosthna24/hcs-lab-automation/actions/workflows/tests.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Lab automation and systems integration for high-content screening. It connects the microscope (ImageXpress with MetaXpress, or Opera Phenix) directly to the analysis pipeline and the reporting. A finished plate is picked up, copied, checked, analysed and reported with no manual steps: an Excel workbook, a GraphPad Prism file, figures, a PowerPoint deck and a written summary.**

I built this during my PhD (UCL) to automate a CFTR drug-screening workflow that runs on two microscopes. Before, every plate meant:
- copying the data by hand
- choosing the right config for the microscope
- typing input files
- manually deleting wells the microscope failed to image
- hours of reporting

Now it runs by itself, and every run leaves a full audit trail.

```
                     ┌──────────────────────────── hcsauto watch ────────────────────────────┐
 microscope output ──►  0 INGEST  ──►  1 PREPARE  ──►  2 ANALYSE  ──►  3 REPORT  ──► Excel · Prism · figures · slides · summary
 (images + MetaXpress)  wait until      detect microscope   your pipeline     stats + plate QC
 plate maps ──────────►  finished,      read MetaXpress      (any command)
                         verified copy  reconcile wells
                                        adapt QC settings
                                        build input file
```

## Systems integration: microscope → MetaXpress → pipeline

| Integration | How |
|---|---|
| **Data transfer** | `watch` monitors the microscope's output folder. When a plate is **finished** (every timepoint in the MetaXpress `.HTD` written, the last timepoint populated, no new files for a settle period), the whole plate folder is copied to the analysis store, **MetaXpress metadata included**. Copies are resumable, written atomically (`.partial` then rename) and verified by size or SHA-256. |
| **MetaXpress metadata drives the set-up** | The `.HTD` file supplies the plate format (96/384), the **wells selected for acquisition**, the number of timepoints and the wavelengths. Together with the TIFF metadata, it decides which microscope and settings apply. |
| **Automatic well reconciliation** | Every planned well is compared across three sources: the **plate map** (what was planned), **MetaXpress** (what the microscope was told to image) and the **image files** (what was actually written). Wells that weren't acquired, or that failed on the microscope (missing frames or channels), are **removed from the input file automatically** and logged in `well_reconciliation.csv`. No more editing input files by hand after a microscope fault. |
| **Plate map matching** | Each plate is paired with `<plate name>.csv` from a plate-map folder (or a default map). A plate with no map is held, not failed. |
| **State tracking** | `hcsauto_state.json` records every plate processed, so nothing runs twice. A failed run is flagged, not silently retried. |

Well statuses in `well_reconciliation.csv`:

| Status | Meaning | Action |
|---|---|---|
| `analysed` | planned, with a complete image set | in the input file |
| `not_acquired` | planned, but not selected in MetaXpress | removed, logged |
| `acquisition_fault` | selected, but frames or channels missing (microscope fault) | removed, logged |
| `unplanned` | imaged, but not on the plate map | ignored, logged for checking |

## What it automates

### Stage 1: PREPARE (`hcsauto prepare`)
| Step | Before (manual) | Now (automated) |
|---|---|---|
| Which microscope? | Choose a config by hand | **Detected** from the folder layout and file naming (Phenix `Index.xml` and `r02c03f01p01-ch1sk10…`, ImageXpress `.HTD` and `TimePoint_N/…_B03_w1.TIF`) |
| Image size, bit depth, pixel size, binning, objective | Look them up and type them in | **Read** from the TIFF headers, Phenix `Index.xml` and ImageXpress MetaMorph tags. The source of every value is recorded. |
| QC thresholds | Remember the Phenix numbers vs the ImageXpress numbers | Instrument **QC profile** loaded automatically ([`profiles.yaml`](hcsauto/profiles.yaml)) |
| Pixel-based settings | Recalculate the cell diameter when binning changes | **Rescaled** automatically: diameter scales with 1/pixel size, minimum object size with 1/pixel area |
| Analysis window | Hope those timepoints were acquired | **Checked** against the timepoints actually present |
| Input file | Type well IDs for every condition | **Built from the plate map**, in the instrument's own well naming (`B02` or `r02c02`) |
| Missing wells | Remove them from the input file by hand after every microscope fault | **Reconciled automatically** across the plate map, MetaXpress and the image files, then removed and logged (see above) |
| Plate map mistakes | Found after the run | **Validated**: controls present, enough replicates, near-duplicate labels flagged |
| Record keeping | None | `run_manifest.json`: tool version, timestamp, plate-map SHA-256, detected metadata, final settings, dropped wells |

### Stage 2: ANALYSE (`--pipeline "…"`)
Runs any analysis command with the generated `{input}` and `{config}`, logs its output to `pipeline.log`, and stops cleanly if it fails. The analysis tool isn't hard-wired in, so the automation works with the lab's Cellpose pipeline, an HPC submission script or a different tool entirely.

### Stage 3: REPORT (`hcsauto report`)
| Output | Contents |
|---|---|
| `analysis_summary.xlsx` | Summary · Per condition (mean, SD, SEM, % of control) · Statistics (Welch t-test vs control, **Benjamini–Hochberg FDR**) · Plate QC (Z′, SSMD, cell counts, **PASS / CHECK / FAIL** colour-coded) · Per well · Run info |
| `prism_tables.pzfx` | **GraphPad Prism project**, one column table per readout with wells as replicates, ready for the lab's Prism graphs and analyses. It's written directly as Prism XML, so Prism doesn't need to be installed. |
| `figures/` | One figure per readout (mean ± SEM with every well shown) and a plate-layout heatmap of cells per well for each plate, as PNG and **editable SVG** |
| `run_report.pptx` | Editable deck: title, summary, plate QC, one slide per figure with a caption |
| `summary.md` | A written summary, e.g. *"2 plates, 64 wells, 5,812 cells… F508del_DMSO (−0.35, q = 1e-08)…"* |

The statistical unit is the **well**, not the cell. Cells in the same well aren't independent replicates, and treating them as such inflates significance.

## Around the run: HPC, QC gating, notifications, history, ELN

| Feature | Option | What it does |
|---|---|---|
| **HPC submission** | `--hpc hpc.yaml` ([example for UCL Myriad](examples/hpc_myriad.yaml)) | Uploads **only the images the input file needs** (planned wells, analysis window, plus the MetaXpress metadata), rewrites paths for the cluster, renders an SGE job script (walltime, memory, cores, modules), runs `qsub`, polls `qstat` and downloads the results. It authenticates with SSH keys through an optional jump host (e.g. a university gateway) and rejects unknown host keys, so it can run unattended. |
| **Automatic plate rejection** | `--zprime-min 0.5 --on-fail flag\|exclude\|stop` | Plates whose control Z′ is below the threshold FAIL QC. **flag**: report everything, with failures marked in the summary, deck title and Excel. **exclude**: drop failed plates from statistics, Prism tables and figures. **stop**: release no results, only the QC workbook (exit code 3). |
| **Notifications** | `--notify notify.yaml` ([example](examples/notify.yaml)) | Email (SMTP/TLS, with the deck attached) and/or a **Microsoft Teams** Adaptive Card through a webhook, sent when a run finishes, is rejected or fails. Credentials come from environment variables, and a mail outage never loses a run. |
| **Run database** | `--db runs.sqlite`, then `hcsauto trend` | Every run's QC (Z′, SSMD, verdict), per-condition results and well reconciliation go into **SQLite**. `trend` draws a **statistical process control chart** (Shewhart ±3 SD limits from baseline runs, plus a 7-point drift rule) to catch assay degradation over months. |
| **ELN / LIMS export** | `--eln` or `hcsauto eln RUN` | `eln_package.zip`: a structured `eln_record.json` and `.md` (operator, instrument, settings, inputs, QC, summary, SHA-256 of every attachment), a `lims_wells.csv` with one row per well, and **RO-Crate 1.1** metadata (the FAIR research-object standard). Ready for LabArchives, Benchling or eLabFTW. |

Query the history directly, e.g. the Z′ per instrument per month:

```sql
SELECT r.instrument, substr(r.recorded_at, 1, 7) AS month, AVG(q.zprime) AS mean_zprime, COUNT(*) AS plates
FROM plate_qc q JOIN runs r USING (run_id)
WHERE q.readout = 'logMemProx'
GROUP BY r.instrument, month ORDER BY month;
```

## Quick start

```bash
pip install -r requirements.txt
python scripts/demo.py          # simulated ImageXpress output, including a microscope fault, processed hands-off -> demo_output/
```

Hands-off, on the lab PC (or as a scheduled task with `--once`):

```bash
python -m hcsauto watch "\\imagexpress\MetaXpress_Images" --dest "D:\analysis_data" --runs "D:\runs" \
    --plate-maps "D:\plate_maps" --norm WT_DMSO --pos WT_DMSO --neg F508del_DMSO \
    --config-template CFTRimg_config_file.csv --hpc hpc_myriad.yaml \
    --zprime-min 0.5 --on-fail flag --notify notify.yaml --db "D:\runs\runs.sqlite" --eln

python -m hcsauto trend "D:\runs\runs.sqlite" --readout logMemProx -o "D:\runs\trends"
```

That single `watch` command covers the whole lifecycle: microscope finishes → verified copy → MetaXpress-driven set-up and well reconciliation → analysis on Myriad → QC gate → Excel, Prism, figures, slides → database → ELN package → email/Teams.

Single plate:

```bash
python -m hcsauto ingest "\\imagexpress\MetaXpress_Images\Plate_286" "D:\analysis_data" --verify sha256
python -m hcsauto detect "D:\analysis_data\Plate_286"           # what is this folder?

python -m hcsauto run plate_map.csv "D:\Plate_286" \
    --norm WT_DMSO --pos WT_DMSO --neg F508del_DMSO \
    --config-template CFTRimg_config_file.csv -o runs/plate286 \
    --pipeline "python main.py --input {input} --config {config} --out {results_dir}"
```

Or run the stages separately (`prepare`, then your analysis, then `report results.xlsx --manifest runs/plate286/run_manifest.json`).

## Adding an instrument or changing thresholds
Edit [`hcsauto/profiles.yaml`](hcsauto/profiles.yaml), or pass `--profiles my_profiles.yaml`. Detection for a new microscope is one function in [`detect.py`](hcsauto/detect.py).

## Engineering
- **Tested end to end:** the test suite builds simulated Phenix and ImageXpress folders, including real TIFFs with instrument metadata. It checks detection, profile selection and rescaling, window validation, dropped-well handling and manifest contents, then runs the full `run` command against a stand-in pipeline and checks every report artefact (the Excel sheets, Prism XML tables, slide count and SVG figures).
- **CI** on every push (GitHub Actions)
- **No real data** in the repository; `.gitignore` blocks images, results and models

```
hcsauto/
  watch.py       hands-off plate processing             ingest.py          completion check + verified copy
  hpc.py         SGE/Myriad upload-submit-poll-download notify.py          email + Teams
  rundb.py       SQLite history + SPC control charts    eln.py             ELN/LIMS + RO-Crate export
  metaxpress.py  MetaXpress .HTD reader                  
  detect.py      microscope detection + metadata        profiles.py/.yaml  instrument QC profiles
  plateplan/     plate map → validated input sheet      prepare.py         stage 1 + manifest
  report/        summarise · prism · figures · slides   __main__.py        CLI: detect / prepare / report / run
  demo.py        synthetic image folders and results
```

## Skills demonstrated
Lab automation and workflow orchestration · systems integration (ImageXpress/MetaXpress → analysis pipeline) · event-driven processing (folder watching, completion detection) · verified, resumable data transfer · HPC job orchestration (SSH, SGE) · QC gating · alerting (SMTP, Teams webhooks) · SQL and statistical process control · ELN/LIMS integration and FAIR data packaging (RO-Crate) · instrument metadata parsing (MetaXpress .HTD, Phenix Index.xml, TIFF tags) · configuration management · data validation · audit trails and reproducibility (GLP-minded) · automated reporting (Excel, GraphPad Prism, PowerPoint) · statistics with multiple-testing correction · testing and CI

---
*Part of my PhD tooling for high-content CFTR screening. See my [GitHub profile](https://github.com/Jyosthna24).*
