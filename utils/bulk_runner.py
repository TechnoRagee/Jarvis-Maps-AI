"""
bulk_runner.py
--------------
Parallel bulk amenity search engine with automatic chunk merging.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from utils.amenity_finder import (
    search_amenities_overpass,
    search_amenities_selenium,
    save_amenities_to_excel,
    save_amenities_to_kml,
    _is_valid_place_name,
)
from utils.excel_handler import get_user_downloads_dir


MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MODULE_DIR.parent
PROGRESS_FILE = str(PROJECT_DIR / "bulk_progress.json")
OUTPUT_DIR = os.path.join(get_user_downloads_dir(), "JarvisMaps_Bulk_Output")
AUTOSAVE_EVERY = 50
MAX_BROWSERS = min(12, max(2, multiprocessing.cpu_count()))


def _safe_filename(value: Any, fallback: str = "item") -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._-")
    return text[:80] or fallback


def _clean_text(value: Any, default: str = "") -> str:
    if value is None or pd.isna(value):
        return default
    text = str(value).strip()
    return text if text and text.lower() != "nan" else default


def _clean_limit(value: Any, default: int = 100) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


def _job_key(job: dict) -> str:
    return "|".join(
        (
            _clean_text(job.get("amenity")).lower(),
            _clean_text(job.get("area")).lower(),
            _clean_text(job.get("district", "")).lower(),
            _clean_text(job.get("level"), "City").lower(),
            str(_clean_limit(job.get("limit"), 100)),
            str(job.get("id", "")),
        )
    )


def _normalize_dataframe_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "run_id": "Run ID",
        "run id": "Run ID",
        "name": "Name",
        "amenity": "Amenity",
        "area": "Area",
        "latitude": "Latitude",
        "longitude": "Longitude",
        "address": "Address",
        "rating": "Rating",
        "phone": "Phone",
        "opening hours": "Opening Hours",
        "opening_hours": "Opening Hours",
        "source file": "Source File",
    }
    rename_map = {}
    for column in dataframe.columns:
        normalized = str(column).strip().lower().replace("-", " ")
        rename_map[column] = aliases.get(normalized, str(column).strip())
    return dataframe.rename(columns=rename_map)


def read_csv_safe(filepath: str) -> pd.DataFrame:
    """Read CSV files handling various Windows/Excel encodings and non-breaking spaces."""
    last_error = None
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(filepath, encoding=enc)
            df.columns = [_clean_text(column) for column in df.columns]
            for column in df.columns:
                if df[column].dtype == object:
                    df[column] = df[column].map(_clean_text)
            return df
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not read CSV file encoding: {last_error}")


def load_jobs_safe(filepath: str) -> list[dict]:
    try:
        dataframe = read_csv_safe(filepath)
        normalized_columns = {
            re.sub(r"[^a-z0-9_]", "_", str(col).lower().strip()).strip("_"): col
            for col in dataframe.columns
        }

        def find_column(*names: str) -> str | None:
            for name in names:
                normalized = re.sub(r"[^a-z0-9_]", "_", name.lower().strip()).strip("_")
                if normalized in normalized_columns:
                    return normalized_columns[normalized]
            return None

        amenity_col = find_column("amenity", "amenity_type", "type", "category")
        area_col = find_column("area", "city", "location", "place")
        district_col = find_column("district", "district_name", "dist")
        level_col = find_column("level", "area_level", "search_level")
        limit_col = find_column("limit", "result_limit", "max_results", "count")

        if not amenity_col or not area_col:
            raise ValueError(
                "Jobs CSV must contain at least these columns: amenity, area. "
                "Optional columns: district, level, limit."
            )

        jobs = []
        for _, row in dataframe.iterrows():
            amenity = _clean_text(row.get(amenity_col))
            area = _clean_text(row.get(area_col))
            if not amenity or not area:
                continue

            jobs.append(
                {
                    "id": len(jobs) + 1,
                    "amenity": amenity,
                    "area": area,
                    "district": _clean_text(row.get(district_col), "") if district_col else "",
                    "level": _clean_text(row.get(level_col), "City") if level_col else "City",
                    "limit": _clean_limit(row.get(limit_col), 100) if limit_col else 100,
                    "status": "pending",
                    "collected": 0,
                }
            )
        return jobs
    except Exception as exc:
        print(f"Error loading jobs: {exc}")
        return []


load_jobs = load_jobs_safe


def create_sample_jobs_csv(filepath: str = "bulk_jobs.csv") -> str:
    sample = pd.DataFrame(
        [
            {"amenity": "hospital", "area": "Delhi", "level": "City", "limit": 100},
            {"amenity": "school", "area": "Mumbai", "level": "City", "limit": 100},
            {"amenity": "bank", "area": "Chennai", "level": "City", "limit": 100},
            {"amenity": "clinic", "area": "Pune", "level": "City", "limit": 100},
        ]
    )
    output = Path(filepath).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(output, index=False)
    print(f"Sample jobs file created: {output}")
    return str(output)


class ProgressManager:
    def __init__(self, jobs: list[dict], progress_file: str = PROGRESS_FILE, run_id: str = ""):
        self.jobs = jobs
        self.run_id = run_id
        self.progress_file = Path(progress_file).resolve()
        self.lock = threading.Lock()
        self.total_done = 0
        for job in self.jobs:
            if not job.get("status"):
                job["status"] = "pending"
            if "collected" not in job:
                job["collected"] = 0
        self._load()

    @staticmethod
    def validate_resume(progress_file: Path | str, jobs: list[dict] = None) -> tuple[bool, str]:
        """Verify that progress.json exists, is valid, and matches jobs before resuming."""
        p = Path(progress_file).resolve()
        if not p.exists():
            return False, f"Progress checkpoint not found at: {p}"
        try:
            with p.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                return False, "Progress checkpoint is invalid (expected JSON dictionary)"
            saved_jobs = data.get("jobs", data)
            if not isinstance(saved_jobs, dict) or not saved_jobs:
                return False, "Progress checkpoint contains no recorded jobs"
            if jobs:
                job_keys = {_job_key(j) for j in jobs}
                overlap = job_keys.intersection(saved_jobs.keys())
                if not overlap:
                    return False, "Checkpoint job keys do not match current loaded jobs"
            return True, "Valid checkpoint"
        except Exception as exc:
            return False, f"Could not read progress checkpoint: {exc}"

    def _load(self) -> None:
        if not self.progress_file.exists():
            return
        try:
            with self.progress_file.open("r", encoding="utf-8") as file:
                saved = json.load(file)

            saved_jobs = saved.get("jobs", saved) if isinstance(saved, dict) else {}

            for job in self.jobs:
                saved_job = saved_jobs.get(_job_key(job))
                if not saved_job or not isinstance(saved_job, dict):
                    continue
                status = str(saved_job.get("status", "pending"))
                if status == "running":
                    status = "pending"
                job["status"] = status
                job["collected"] = max(0, int(saved_job.get("collected", 0) or 0))

            self.total_done = sum(1 for job in self.jobs if job.get("status") == "done")
            print(f"Resumed from checkpoint — {self.total_done} jobs already done")
        except Exception as exc:
            print(f"Could not load progress file: {exc}")

    def save(self) -> None:
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            data = {
                "_metadata": {
                    "run_id": self.run_id,
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
                "jobs": {
                    _job_key(job): {
                        "status": job.get("status", "pending"),
                        "collected": int(job.get("collected", 0) or 0),
                    }
                    for job in self.jobs
                }
            }
            temp_file = self.progress_file.with_suffix(".json.tmp")
            with temp_file.open("w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
            os.replace(temp_file, self.progress_file)

    def mark_running(self, job: dict) -> None:
        with self.lock:
            job["status"] = "running"
        self.save()

    def update_collected(self, job: dict, count: int, *, save: bool = False) -> None:
        with self.lock:
            job["collected"] = max(0, int(count))
        if save:
            self.save()

    def mark_done(self, job: dict, count: int) -> None:
        with self.lock:
            job["status"] = "done"
            job["collected"] = max(0, int(count))
            self.total_done = sum(1 for item in self.jobs if item.get("status") == "done")
        self.save()

    def mark_failed(self, job: dict) -> None:
        with self.lock:
            job["status"] = "failed"
        self.save()

    def get_pending_jobs(self) -> list[dict]:
        with self.lock:
            return [
                job
                for job in self.jobs
                if job.get("status") in ("pending", "failed")
            ]

    def count_done(self) -> int:
        with self.lock:
            return sum(1 for job in self.jobs if job.get("status") == "done")

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(job) for job in self.jobs]

    def reset(self) -> None:
        try:
            self.progress_file.unlink(missing_ok=True)
        except OSError:
            pass
        with self.lock:
            for job in self.jobs:
                job["status"] = "pending"
                job["collected"] = 0
            self.total_done = 0
        self.save()


def run_job(
    job: dict,
    run_dir: str | Path,
    autosave_every: int,
    progress: ProgressManager,
    run_id: str = "",
    log_callback: Optional[Callable] = None,
    status_callback: Optional[Callable] = None,
    headless: bool | None = None,
) -> list[dict]:
    def log(message: str, tag: str = "normal") -> None:
        prefix = f"[{job['amenity']}/{job['area']}]"
        full_msg = f"{prefix} {message}"
        print(full_msg)
        try:
            log_file = chunks_dir.parent / "bulk_run.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().strftime('%H:%M:%S')} {full_msg}\n")
        except Exception:
            pass
        if log_callback:
            log_callback(f"{full_msg}\n", tag)

    def update_status(message: str) -> None:
        if status_callback:
            status_callback(job["id"], message)

    amenity = job["amenity"]
    area = job["area"]
    district = job.get("district", "")
    limit = job["limit"]

    run_path = Path(run_dir).resolve()
    chunks_path = run_path / "chunks"
    chunks_path.mkdir(parents=True, exist_ok=True)
    autosave_every = max(1, int(autosave_every))

    log(f"Starting: {amenity} in {area} (limit: {limit}) [Run: {run_id}]", "info")
    progress.mark_running(job)
    update_status("running")

    results = []
    last_checkpoint_count = 0

    def on_progress(count: int) -> None:
        nonlocal last_checkpoint_count
        count = max(0, int(count))
        should_save = count == 1 or count - last_checkpoint_count >= 10
        progress.update_collected(job, count, save=should_save)
        if should_save:
            last_checkpoint_count = count
        update_status(f"running|{count}")

    try:
        qualified_query = f"{amenity} in {area}"
        if district:
            qualified_query = f"{amenity} in {area}, {district}"

        all_results = search_amenities_selenium(
            qualified_query,
            amenity,
            area,
            limit,
            log,
            on_progress,
            district=district,
            headless=headless,
        )

        if not all_results:
            log("Selenium returned 0 results — trying Overpass", "warn")
            all_results = search_amenities_overpass(amenity, area, limit, log, district=district)

        all_results = list(all_results or [])
        valid_records = []
        for record in all_results:
            if (
                record.get("latitude") is not None
                and record.get("longitude") is not None
                and _is_valid_place_name(record.get("name", ""))
            ):
                rec = dict(record)
                if run_id:
                    rec["run_id"] = run_id
                valid_records.append(rec)
        all_results = valid_records

        safe_amenity = _safe_filename(amenity, "amenity")
        safe_area = _safe_filename(area, "area")
        job_prefix = f"job{int(job.get('id', 0)):03d}_{safe_amenity}_{safe_area}"

        # Clean up prior chunks for THIS specific job in THIS run only (intra-run protection)
        for old_chunk in chunks_path.glob(f"{job_prefix}_chunk*.xlsx"):
            try:
                old_chunk.unlink()
            except OSError:
                pass

        for chunk_number, start in enumerate(
            range(0, len(all_results), autosave_every),
            start=1,
        ):
            chunk = all_results[start : start + autosave_every]
            chunk_file = chunks_path / f"{job_prefix}_chunk{chunk_number:04d}.xlsx"
            if chunk_file.exists():
                chunk_file = chunks_path / f"{job_prefix}_chunk{chunk_number:04d}_{uuid.uuid4().hex[:6]}.xlsx"

            saved_path = save_amenities_to_excel(
                chunk,
                amenity,
                area,
                str(chunk_file),
                run_id=run_id,
            )

            if not Path(saved_path).exists():
                raise FileNotFoundError(f"Chunk was not created: {saved_path}")

            results.extend(chunk)
            log(
                f"Auto-saved chunk {chunk_number} ({len(chunk)} records)",
                "success",
            )
            progress.update_collected(job, len(results), save=True)
            update_status(f"running|{len(results)}")

        progress.mark_done(job, len(results))
        log(f"Done — {len(results)} records collected", "success")
        update_status(f"done|{len(results)}")

    except Exception as exc:
        log(f"Failed [{type(exc).__name__}]: {exc}", "error")
        print(traceback.format_exc())
        progress.mark_failed(job)
        update_status("failed")

    return results


class BulkRunner:
    @staticmethod
    def generate_run_id() -> str:
        """Generate a collision-proof run_id: timestamp + 8-character unique hex token."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        return f"run_{timestamp}_{token}"

    def __init__(
        self,
        jobs: list[dict],
        max_browsers: int = MAX_BROWSERS,
        autosave_every: int = AUTOSAVE_EVERY,
        output_dir: str = OUTPUT_DIR,
        log_callback: Optional[Callable] = None,
        status_callback: Optional[Callable] = None,
        progress_callback: Optional[Callable] = None,
        done_callback: Optional[Callable] = None,
        cleanup_chunks: bool = False,
        run_id: Optional[str] = None,
        headless: bool | None = None,
    ):
        self.jobs = jobs
        self.max_browsers = max(1, min(12, int(max_browsers)))
        self.autosave_every = max(1, int(autosave_every))
        self.base_output_dir = Path(output_dir).resolve()
        self.headless = headless

        # Rule 1: Centralized, collision-proof run_id
        if run_id and str(run_id).strip():
            self.run_id = str(run_id).strip()
        else:
            self.run_id = self.generate_run_id()

        # Rule 2: Dedicated isolated directory tree for this run
        self.run_dir = self.base_output_dir / "runs" / self.run_id
        self.chunks_dir = self.run_dir / "chunks"
        self.districts_dir = self.run_dir / "districts"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.districts_dir.mkdir(parents=True, exist_ok=True)

        self.progress_file = self.run_dir / "progress.json"

        self.log_callback = log_callback
        self.status_callback = status_callback
        self.progress_callback = progress_callback
        self.done_callback = done_callback
        self.cleanup_chunks = bool(cleanup_chunks)

        self.progress = ProgressManager(
            jobs=jobs,
            progress_file=str(self.progress_file),
            run_id=self.run_id,
        )
        self.all_results = []
        self.results_lock = threading.Lock()
        self._stop_flag = False
        self._running = False
        self.start_time = None
        self.final_excel_path = None
        self.final_kml_path = None

        print(f"Bulk run directory [{self.run_id}]: {self.run_dir}")

    def log(self, message: str, tag: str = "normal") -> None:
        print(message)
        try:
            log_file = self.run_dir / "bulk_run.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.utcnow().strftime('%H:%M:%S')} {message}\n")
        except Exception:
            pass
        if self.log_callback:
            self.log_callback(f"{message}\n", tag)

    def start(self, resume: bool = True) -> None:
        if self._running:
            return

        self._stop_flag = False
        self._running = True
        self.start_time = time.time()
        with self.results_lock:
            self.all_results = []

        if not resume:
            self.progress.reset()
            self.log(f"Starting fresh run [{self.run_id}]", "info")
        else:
            # Rule 3 & User Addition Point 2: Validate resume strictly
            valid, msg = ProgressManager.validate_resume(self.progress_file, self.jobs)
            if not valid:
                self._running = False
                err_msg = f"Cannot resume run [{self.run_id}]: {msg}"
                self.log(f"❌ {err_msg}", "error")
                raise ValueError(err_msg)
            pending = self.progress.get_pending_jobs()
            self.log(f"Resuming run [{self.run_id}] — {len(pending)} jobs pending", "info")

        threading.Thread(target=self._run_all, daemon=True).start()

    def stop(self) -> None:
        self._stop_flag = True
        self.log("Stop requested — finishing current jobs", "warn")

    def _run_all(self) -> None:
        pending = self.progress.get_pending_jobs()

        if not pending:
            self.log("All jobs are already completed", "success")
            self._running = False
            self._finalize()
            return

        semaphore = threading.Semaphore(self.max_browsers)
        threads = []

        def worker(job: dict) -> None:
            try:
                results = run_job(
                    job,
                    self.run_dir,
                    self.autosave_every,
                    self.progress,
                    run_id=self.run_id,
                    log_callback=self.log_callback,
                    status_callback=self.status_callback,
                    headless=self.headless,
                )
                with self.results_lock:
                    self.all_results.extend(results)
            except Exception as exc:
                self.log(f"Unexpected job error: {exc}", "error")
            finally:
                semaphore.release()
                self._update_progress(
                    self.progress.count_done(),
                    len(self.jobs),
                )

        try:
            for job in pending:
                if self._stop_flag:
                    break

                semaphore.acquire()
                thread = threading.Thread(
                    target=worker,
                    args=(job,),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
                time.sleep(0.5)

            for thread in threads:
                thread.join()

            self._finalize()

        except Exception as exc:
            self.log(f"Bulk runner failed: {exc}", "error")
            print(traceback.format_exc())
            if self.done_callback:
                self.done_callback([])
        finally:
            self._running = False

    def _update_progress(self, done: int, total: int) -> None:
        if not self.progress_callback:
            return

        total = max(1, int(total))
        done = max(0, min(int(done), total))
        percentage = (done / total) * 100.0
        elapsed = time.time() - self.start_time if self.start_time else 0.0
        remaining = max(total - done, 0)
        eta = (elapsed / max(done, 1)) * remaining
        self.progress_callback(done, total, percentage, eta)

    def _finalize(self) -> None:
        self.log(f"\nFinalizing results for run [{self.run_id}]...", "info")

        # Rule 5 & 10: Scan ONLY this run's chunks directory!
        chunk_files = sorted(self.chunks_dir.glob("*.xlsx"))

        self.log(f"Run folder: {self.run_dir}", "info")
        self.log(f"Chunks found for this run: {len(chunk_files)}", "info")

        dataframes = []

        for chunk_file in chunk_files:
            try:
                dataframe = pd.read_excel(chunk_file, engine="openpyxl")
                if dataframe.empty:
                    continue
                dataframe = _normalize_dataframe_columns(dataframe)
                dataframe["Source File"] = chunk_file.name
                if "Run ID" not in dataframe.columns and "run_id" not in dataframe.columns:
                    dataframe["Run ID"] = self.run_id
                dataframes.append(dataframe)
                self.log(
                    f"Loaded {chunk_file.name} ({len(dataframe)} records)",
                    "info",
                )
            except Exception as exc:
                self.log(f"Could not read {chunk_file.name}: {exc}", "error")

        if not dataframes:
            with self.results_lock:
                memory_results = list(self.all_results)
            if memory_results:
                dataframe = _normalize_dataframe_columns(pd.DataFrame(memory_results))
                dataframe["Source File"] = "in_memory"
                if "Run ID" not in dataframe.columns:
                    dataframe["Run ID"] = self.run_id
                dataframes.append(dataframe)
                self.log(
                    "Using in-memory results because no readable chunks were found",
                    "warn",
                )

        if not dataframes:
            self.log("No records were available to merge for this run", "warn")
            if self.done_callback:
                self.done_callback([])
            return

        merged = pd.concat(dataframes, ignore_index=True, sort=False)

        # Standardize Run ID column
        if "run_id" in merged.columns and "Run ID" not in merged.columns:
            merged = merged.rename(columns={"run_id": "Run ID"})
        elif "Run ID" not in merged.columns:
            merged["Run ID"] = self.run_id

        for column in ("Latitude", "Longitude"):
            if column in merged.columns:
                merged[column] = pd.to_numeric(merged[column], errors="coerce")

        if "Latitude" in merged.columns and "Longitude" in merged.columns:
            merged = merged.dropna(subset=["Latitude", "Longitude"])

        if "Name" in merged.columns:
            merged = merged[merged["Name"].astype(str).map(_is_valid_place_name)]

        duplicate_columns = [
            column
            for column in ("Name", "Latitude", "Longitude")
            if column in merged.columns
        ]
        if duplicate_columns:
            merged = merged.drop_duplicates(subset=duplicate_columns, keep="first")
        else:
            merged = merged.drop_duplicates(keep="first")

        # ── 1. Create Per-District Separate Workbooks in districts/ (Rule 5 & Requirement 3) ──
        if "Area" in merged.columns:
            for area_val, district_df in merged.groupby("Area"):
                safe_area_name = _safe_filename(area_val, "district")
                dist_file = self.districts_dir / f"{safe_area_name}_results.xlsx"
                try:
                    with pd.ExcelWriter(dist_file, engine="openpyxl") as d_writer:
                        district_df.to_excel(d_writer, sheet_name=str(area_val)[:31], index=False)
                except Exception as e:
                    self.log(f"Warning saving district file {dist_file.name}: {e}", "warn")

        # ── 2. Create Consolidated Excel for THIS RUN ONLY (Rule 5) ─────────────
        final_excel = self.run_dir / f"bulk_run_{self.run_id}.xlsx"
        final_kml = self.run_dir / f"bulk_run_{self.run_id}.kml"

        with pd.ExcelWriter(final_excel, engine="openpyxl") as writer:
            # Sheet 1: All results for this run
            merged.to_excel(writer, sheet_name="All Results", index=False)
            ws_all = writer.sheets["All Results"]
            ws_all.freeze_panes = "A2"
            ws_all.auto_filter.ref = ws_all.dimensions

            # Sheets 2..N: Individual district sheets
            if "Area" in merged.columns:
                seen_sheets = {"all results"}
                for area_val, district_df in merged.groupby("Area"):
                    sheet_title = str(area_val).strip()[:28] or "District"
                    counter = 1
                    base_title = sheet_title
                    while sheet_title.lower() in seen_sheets:
                        sheet_title = f"{base_title[:25]}_{counter}"
                        counter += 1
                    seen_sheets.add(sheet_title.lower())
                    district_df.to_excel(writer, sheet_name=sheet_title, index=False)

            # Auto-fit column widths across all sheets
            for sheet_name in writer.sheets:
                ws = writer.sheets[sheet_name]
                for column_cells in ws.columns:
                    maximum_length = max(
                        len(str(cell.value)) if cell.value is not None else 0
                        for cell in column_cells
                    )
                    ws.column_dimensions[column_cells[0].column_letter].width = min(
                        maximum_length + 4, 50
                    )

        self.final_excel_path = str(final_excel)
        self.log(
            f"Consolidated Excel: {final_excel.name} ({len(merged)} records)",
            "success",
        )

        # ── 3. Create Consolidated KML for THIS RUN ONLY (Rule 5) ────────────────
        results_list = merged.to_dict(orient="records")
        kml_results = []
        for record in results_list:
            latitude = record.get("Latitude")
            longitude = record.get("Longitude")
            if pd.isna(latitude) or pd.isna(longitude):
                continue
            kml_results.append(
                {
                    "name": record.get("Name", "Unknown"),
                    "amenity": record.get("Amenity", ""),
                    "latitude": latitude,
                    "longitude": longitude,
                    "address": record.get("Address", "N/A"),
                    "rating": record.get("Rating", "N/A"),
                    "phone": record.get("Phone", "N/A"),
                    "opening_hours": record.get("Opening Hours", "N/A"),
                }
            )

        if kml_results:
            try:
                save_amenities_to_kml(
                    kml_results,
                    "amenities",
                    self.run_id,
                    str(final_kml),
                )
                self.final_kml_path = str(final_kml)
                self.log(f"Consolidated KML: {final_kml.name}", "success")
            except Exception as exc:
                self.log(f"Could not save final KML: {exc}", "warn")

        if self.cleanup_chunks:
            for chunk_file in chunk_files:
                try:
                    chunk_file.unlink()
                except OSError:
                    pass

        self.log(f"Run [{self.run_id}] completed successfully!\n", "success")

        if self.done_callback:
            self.done_callback(results_list)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_stats(self) -> dict:
        jobs = self.progress.snapshot()
        total = len(jobs)
        done = sum(1 for job in jobs if job.get("status") == "done")
        failed = sum(1 for job in jobs if job.get("status") == "failed")
        running = sum(1 for job in jobs if job.get("status") == "running")
        pending = max(total - done - failed - running, 0)
        collected = sum(int(job.get("collected", 0) or 0) for job in jobs)

        elapsed = time.time() - self.start_time if self.start_time else 0.0
        eta = (elapsed / max(done, 1)) * pending if done else 0.0

        return {
            "total": total,
            "done": done,
            "failed": failed,
            "running": running,
            "pending": pending,
            "collected": collected,
            "elapsed": elapsed,
            "eta": eta,
        }

    @staticmethod
    def format_time(seconds: float) -> str:
        seconds = max(0, float(seconds))
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"