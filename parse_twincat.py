#!/usr/bin/env python3
"""
parse_twincat.py – TwinCAT Project Parser for CompanyGPT Knowledge Base
==========================================================================
Recursively scans a TwinCAT repository, parses all relevant project files
(.tsproj, .TcPOU, .TcGVL, .TcDUT, .TcIO, .tmc), generates a structured
Markdown document, and uploads it to the CompanyGPT Files API (KB-Agent-TwinCAT).

Usage:
    python scripts/parse_twincat.py

Required environment variables:
    COMPANYGPT_API_KEY   – Bearer token for CompanyGPT Files API
    COMPANYGPT_API_URL   – API endpoint URL (set as GitHub Variable)

Exit codes:
    0 – Success
    1 – Critical error
"""

import os
import re
import sys
import json
import time
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# =============================================================================
# CONFIGURATION
# =============================================================================

COMPANYGPT_API_KEY: str = os.environ.get("COMPANYGPT_API_KEY", "")
COMPANYGPT_API_URL: str = os.environ.get("COMPANYGPT_API_URL", "")  # Set via GitHub Variable
KB_FOLDER: str = "KB-Agent-TwinCAT"
OUTPUT_FILENAME: str = "twincat_structure.md"
REPO_ROOT: Path = Path(__file__).parent.parent.resolve()  # scripts/ → repo root

UPLOAD_MAX_RETRIES: int = 3
UPLOAD_RETRY_DELAY: int = 5

TWINCAT_EXTENSIONS: dict[str, str] = {
    ".tsproj": "project",
    ".TcPOU":  "pou",
    ".TcGVL":  "gvl",
    ".TcDUT":  "dut",
    ".TcIO":   "io",
    ".tmc":    "library",
}

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("parse_twincat")

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class Variable:
    name: str
    data_type: str
    initial_value: str = ""
    comment: str = ""
    section: str = ""

@dataclass
class POU:
    name: str
    pou_type: str
    file_path: str
    variables: list[Variable] = field(default_factory=list)
    description: str = ""
    return_type: str = ""

@dataclass
class GVL:
    name: str
    file_path: str
    variables: list[Variable] = field(default_factory=list)

@dataclass
class DUT:
    name: str
    dut_type: str
    file_path: str
    members: list[Variable] = field(default_factory=list)

@dataclass
class Library:
    name: str
    version: str
    namespace: str = ""
    source_file: str = ""

@dataclass
class Task:
    name: str
    cycle_time: str = ""
    priority: str = ""
    pous: list[str] = field(default_factory=list)

@dataclass
class Axis:
    name: str
    axis_type: str = ""
    source_file: str = ""

@dataclass
class TwinCATProject:
    project_name: str = ""
    pous: list[POU] = field(default_factory=list)
    gvls: list[GVL] = field(default_factory=list)
    duts: list[DUT] = field(default_factory=list)
    libraries: list[Library] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    axes: list[Axis] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

# =============================================================================
# STEP 1 – FILE DISCOVERY
# =============================================================================

def find_twincat_files(root: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {cat: [] for cat in TWINCAT_EXTENSIONS.values()}
    log.info("Scanning for TwinCAT files in: %s", root)
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if any(part.startswith(".") for part in file_path.parts):
            continue
        suffix = file_path.suffix
        for ext, category in TWINCAT_EXTENSIONS.items():
            if suffix.lower() == ext.lower():
                found[category].append(file_path)
                break
    for category, files in found.items():
        log.info("  %-10s: %d file(s)", category, len(files))
    return found

# =============================================================================
# STEP 2 – PARSING UTILITIES
# =============================================================================

_VAR_SECTION_RE = re.compile(
    r"(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL(?:\s+CONSTANT)?|VAR\b)(.*?)END_VAR",
    re.DOTALL | re.IGNORECASE,
)
_VAR_LINE_RE = re.compile(
    r"^\s*(\w[\w.]*)\s*:\s*((?:[A-Za-z_][\w.]*)(?:\s*\[[^\]]*\])?(?:\s+OF\s+[\w.]+)?(?:\s+TO\s+[\w.]+)?[\w.\s()]*?)"
    r"(?:\s*:=\s*([^;(]+?))?\s*(?:\(\*\s*(.*?)\s*\*\))?\s*;",
    re.MULTILINE | re.IGNORECASE,
)
_POU_PROGRAM_RE = re.compile(r"^\s*PROGRAM\s+(\w+)", re.MULTILINE | re.IGNORECASE)
_POU_FB_RE = re.compile(r"^\s*FUNCTION_BLOCK\s+(\w+)", re.MULTILINE | re.IGNORECASE)
_POU_FUNC_RE = re.compile(r"^\s*FUNCTION\s+(\w+)\s*(?::\s*(\w[\w.]*))?", re.MULTILINE | re.IGNORECASE)
_STRUCT_BODY_RE = re.compile(r"(?:STRUCT|UNION)\s+(.*?)\s*END_(?:STRUCT|UNION)", re.DOTALL | re.IGNORECASE)
_ENUM_BODY_RE = re.compile(r":\s*\(\s*(.*?)\s*\)", re.DOTALL)
_ENUM_MEMBER_RE = re.compile(r"^\s*(\w+)\s*(?::=\s*([^,;(*\n]+?))?\s*(?:\(\*\s*(.*?)\s*\*\))?\s*[,;]", re.MULTILINE)

_RESERVED = frozenset({
    "END_VAR", "VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_GLOBAL",
    "PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "END_PROGRAM", "END_FUNCTION_BLOCK",
    "END_FUNCTION", "STRUCT", "END_STRUCT", "UNION", "END_UNION", "ENUM", "END_ENUM",
    "END_TYPE", "TYPE", "CONSTANT", "RETAIN", "PERSISTENT",
})

def _normalise_section(keyword: str) -> str:
    kw = keyword.strip().upper()
    if kw.startswith("VAR_INPUT"):  return "VAR_INPUT"
    if kw.startswith("VAR_OUTPUT"): return "VAR_OUTPUT"
    if kw.startswith("VAR_IN_OUT"): return "VAR_IN_OUT"
    if "VAR_GLOBAL" in kw:          return "VAR_GLOBAL"
    return "VAR"

def _safe_xml(file_path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(file_path).getroot()
    except (ET.ParseError, OSError) as exc:
        log.warning("XML error in %s: %s", file_path.name, exc)
    return None

def _text(elem: Optional[ET.Element]) -> str:
    return (elem.text or "").strip() if elem is not None else ""

def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag

def _find_child(parent: ET.Element, local_name: str) -> Optional[ET.Element]:
    for child in parent:
        if _local(child.tag) == local_name:
            return child
    return None

def _iter_local(root: ET.Element, local_name: str):
    for elem in root.iter():
        if _local(elem.tag) == local_name:
            yield elem

# =============================================================================
# STEP 2 – IEC 61131-3 PARSERS
# =============================================================================

def parse_variables(declaration: str, allowed_sections: Optional[list[str]] = None) -> list[Variable]:
    variables: list[Variable] = []
    for sect_match in _VAR_SECTION_RE.finditer(declaration):
        section = _normalise_section(sect_match.group(1))
        if allowed_sections and section not in allowed_sections:
            continue
        body = sect_match.group(2)
        for vm in _VAR_LINE_RE.finditer(body):
            name = vm.group(1).strip()
            if name.upper() in _RESERVED:
                continue
            variables.append(Variable(
                name=name,
                data_type=re.sub(r"\s+", " ", vm.group(2).strip()),
                initial_value=(vm.group(3) or "").strip(),
                comment=(vm.group(4) or "").strip(),
                section=section,
            ))
    return variables

def detect_pou_type(declaration: str, fallback_name: str) -> tuple[str, str, str]:
    m = _POU_FUNC_RE.search(declaration)
    if m and not _POU_FB_RE.search(declaration) and not _POU_PROGRAM_RE.search(declaration):
        return "FUNCTION", m.group(1), (m.group(2) or "")
    m = _POU_FB_RE.search(declaration)
    if m: return "FUNCTION_BLOCK", m.group(1), ""
    m = _POU_PROGRAM_RE.search(declaration)
    if m: return "PROGRAM", m.group(1), ""
    return "UNKNOWN", fallback_name, ""

def detect_dut_type(declaration: str) -> str:
    upper = declaration.upper()
    if re.search(r"\bSTRUCT\b", upper): return "STRUCT"
    if re.search(r"\bUNION\b", upper):  return "UNION"
    if re.search(r"\bENUM\b", upper) or _ENUM_BODY_RE.search(declaration): return "ENUM"
    return "UNKNOWN"

def parse_struct_members(declaration: str) -> list[Variable]:
    members: list[Variable] = []
    body_m = _STRUCT_BODY_RE.search(declaration)
    body = body_m.group(1) if body_m else declaration
    for vm in _VAR_LINE_RE.finditer(body):
        name = vm.group(1).strip()
        if name.upper() in _RESERVED: continue
        members.append(Variable(name=name, data_type=re.sub(r"\s+", " ", vm.group(2).strip()),
                                initial_value=(vm.group(3) or "").strip(), comment=(vm.group(4) or "").strip()))
    return members

def parse_enum_members(declaration: str) -> list[Variable]:
    members: list[Variable] = []
    body_m = _ENUM_BODY_RE.search(declaration)
    if not body_m: return members
    for m in _ENUM_MEMBER_RE.finditer(body_m.group(1) + ";"):
        name = m.group(1).strip()
        if name.upper() in _RESERVED: continue
        members.append(Variable(name=name, data_type="ENUM_MEMBER",
                                initial_value=(m.group(2) or "").strip(), comment=(m.group(3) or "").strip()))
    return members

# =============================================================================
# STEP 2 – FILE-SPECIFIC PARSERS
# =============================================================================

def parse_tcpou(file_path: Path, repo_root: Path) -> list[POU]:
    pous: list[POU] = []
    root = _safe_xml(file_path)
    if root is None: return pous
    rel = str(file_path.relative_to(repo_root))
    for pou_elem in _iter_local(root, "POU"):
        xml_name = pou_elem.get("Name", file_path.stem)
        declaration = _text(_find_child(pou_elem, "Declaration"))
        pou_type, name, return_type = detect_pou_type(declaration, xml_name)
        pous.append(POU(name=name or xml_name, pou_type=pou_type, file_path=rel,
                        variables=parse_variables(declaration), return_type=return_type))
    return pous

def parse_tcgvl(file_path: Path, repo_root: Path) -> Optional[GVL]:
    root = _safe_xml(file_path)
    if root is None: return None
    rel = str(file_path.relative_to(repo_root))
    gvl_elem = next(_iter_local(root, "GVL"), root)
    name = gvl_elem.get("Name", file_path.stem)
    declaration = _text(_find_child(gvl_elem, "Declaration"))
    return GVL(name=name, file_path=rel, variables=parse_variables(declaration, ["VAR_GLOBAL", "VAR"]))

def parse_tcdut(file_path: Path, repo_root: Path) -> Optional[DUT]:
    root = _safe_xml(file_path)
    if root is None: return None
    rel = str(file_path.relative_to(repo_root))
    dut_elem = next(_iter_local(root, "DUT"), root)
    name = dut_elem.get("Name", file_path.stem)
    declaration = _text(_find_child(dut_elem, "Declaration"))
    dut_type = detect_dut_type(declaration)
    members = parse_enum_members(declaration) if dut_type == "ENUM" else parse_struct_members(declaration)
    return DUT(name=name, dut_type=dut_type, file_path=rel, members=members)

def _parse_library_string(raw: str, source_file: str, namespace: str = "") -> Optional[Library]:
    raw = raw.strip()
    if not raw: return None
    if "," in raw:
        parts = [p.strip() for p in raw.split(",", 1)]
        lib_name = parts[0]
        ver_m = re.match(r"([*\d][\d.]*|\*)", parts[1]) if len(parts) > 1 else None
        version = "latest" if (ver_m and ver_m.group(1) == "*") else (ver_m.group(1) if ver_m else "unknown")
    else:
        lib_name, version = raw, "unknown"
    return Library(name=lib_name, version=version, namespace=namespace, source_file=source_file)

def parse_tsproj(file_path: Path, repo_root: Path) -> tuple[str, list[Library], list[Task], list[Axis]]:
    project_name = file_path.stem
    libraries, tasks, axes = [], [], []
    root = _safe_xml(file_path)
    if root is None: return project_name, libraries, tasks, axes
    rel = str(file_path.relative_to(repo_root))
    for task_elem in _iter_local(root, "Task"):
        task_name = task_elem.get("Name", "") or _text(_find_child(task_elem, "Name"))
        if not task_name: continue
        tasks.append(Task(
            name=task_name,
            cycle_time=_text(_find_child(task_elem, "CycleTime")),
            priority=_text(_find_child(task_elem, "Priority")),
            pous=[_text(p) for p in _iter_local(task_elem, "POUName") if _text(p)],
        ))
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag in ("LibraryReference", "PlaceholderReference"):
            raw = elem.get("Include") or elem.get("DefaultResolution") or ""
            lib = _parse_library_string(raw, rel, elem.get("Namespace", ""))
            if lib: libraries.append(lib)
        if tag in ("Axis", "NC_Axis"):
            ax_name = elem.get("Name", "")
            if ax_name: axes.append(Axis(name=ax_name, axis_type=elem.get("Type", ""), source_file=rel))
    return project_name, libraries, tasks, axes

def parse_tmc(file_path: Path, repo_root: Path) -> list[Library]:
    libraries = []
    root = _safe_xml(file_path)
    if root is None: return libraries
    rel = str(file_path.relative_to(repo_root))
    for elem in root.iter():
        if _local(elem.tag) in ("Library", "LibraryInfo", "LibRef", "LibraryReference"):
            name = elem.get("Name") or elem.get("Include") or _text(_find_child(elem, "Name"))
            version = elem.get("Version") or _text(_find_child(elem, "Version")) or "unknown"
            if name: libraries.append(Library(name=name, version=version,
                                               namespace=elem.get("Namespace", ""), source_file=rel))
    return libraries

def parse_tcio(file_path: Path, repo_root: Path) -> list[Axis]:
    axes = []
    root = _safe_xml(file_path)
    if root is None: return axes
    rel = str(file_path.relative_to(repo_root))
    for elem in root.iter():
        if _local(elem.tag) in ("Axis", "Drive", "Device"):
            ax_name = elem.get("Name", "")
            if ax_name: axes.append(Axis(name=ax_name, axis_type=elem.get("Type", ""), source_file=rel))
    return axes

# =============================================================================
# STEP 2 – ORCHESTRATOR
# =============================================================================

def parse_all(files: dict[str, list[Path]], repo_root: Path) -> TwinCATProject:
    project = TwinCATProject()

    def _try(label, fn, *args):
        try: return fn(*args)
        except Exception as exc:
            msg = f"Error processing {label}: {exc}"
            log.error(msg); project.parse_errors.append(msg)
            return None

    for f in files.get("project", []):
        result = _try(f.name, parse_tsproj, f, repo_root)
        if result:
            pn, libs, tasks, axes = result
            if not project.project_name: project.project_name = pn
            project.libraries.extend(libs); project.tasks.extend(tasks); project.axes.extend(axes)

    for f in files.get("library", []):
        result = _try(f.name, parse_tmc, f, repo_root)
        if result: project.libraries.extend(result)

    for f in files.get("pou", []):
        result = _try(f.name, parse_tcpou, f, repo_root)
        if result: project.pous.extend(result)

    for f in files.get("gvl", []):
        result = _try(f.name, parse_tcgvl, f, repo_root)
        if result: project.gvls.append(result)

    for f in files.get("dut", []):
        result = _try(f.name, parse_tcdut, f, repo_root)
        if result: project.duts.append(result)

    for f in files.get("io", []):
        result = _try(f.name, parse_tcio, f, repo_root)
        if result: project.axes.extend(result)

    # Deduplicate libraries and axes
    seen_libs: set = set()
    project.libraries = [l for l in project.libraries if not (seen_libs.add((l.name.lower(), l.version)) or (l.name.lower(), l.version) in seen_libs - {(l.name.lower(), l.version)})]
    seen_axes: set = set()
    project.axes = [a for a in project.axes if not (seen_axes.add(a.name.lower()) or a.name.lower() in seen_axes - {a.name.lower()})]

    if not project.project_name: project.project_name = repo_root.name
    log.info("Parsing complete → POUs: %d | GVLs: %d | DUTs: %d | Libs: %d | Tasks: %d | Axes: %d | Errors: %d",
             len(project.pous), len(project.gvls), len(project.duts),
             len(project.libraries), len(project.tasks), len(project.axes), len(project.parse_errors))
    return project

# =============================================================================
# STEP 3 – MARKDOWN GENERATION
# =============================================================================

def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")

def _var_table(variables: list[Variable]) -> str:
    if not variables: return "_No variables declared._\n\n"
    header  = "| Name | Type | Default | Comment |"
    divider = "|------|------|---------|---------|"
    rows = [f"| `{_md_escape(v.name)}` | `{_md_escape(v.data_type)}` | `{_md_escape(v.initial_value)}` | {_md_escape(v.comment)} |"
            for v in variables]
    return "\n".join([header, divider, *rows]) + "\n\n"

def generate_markdown(project: TwinCATProject) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md: list[str] = []

    md.append(f"# TwinCAT Project: `{project.project_name}`\n")
    md.append(f"> 🕒 **Last updated:** {now}  ")
    md.append("> 📦 **Auto-generated by `parse_twincat.py` (GitHub Action)**  ")
    md.append("> 🔗 **Repository:** SoKI_API\n")

    md.append("## 📊 Overview\n")
    md.append("| Category | Count |")
    md.append("|----------|------:|")
    md.append(f"| POUs | **{len(project.pous)}** |")
    md.append(f"| GVLs | **{len(project.gvls)}** |")
    md.append(f"| DUTs | **{len(project.duts)}** |")
    md.append(f"| Libraries | **{len(project.libraries)}** |")
    md.append(f"| Tasks | **{len(project.tasks)}** |")
    md.append(f"| Axes | **{len(project.axes)}** |")
    md.append("")

    md.append("---\n")
    md.append("## 🔷 POUs\n")
    pous_by_type: dict[str, list[POU]] = {}
    for pou in sorted(project.pous, key=lambda p: (p.pou_type, p.name)):
        pous_by_type.setdefault(pou.pou_type, []).append(pou)
    type_meta = {"PROGRAM": "▶️ Programs", "FUNCTION_BLOCK": "📦 Function Blocks",
                 "FUNCTION": "⚙️ Functions", "UNKNOWN": "❓ Unknown"}
    for pou_type in ("PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "UNKNOWN"):
        group = pous_by_type.get(pou_type, [])
        if not group: continue
        md.append(f"### {type_meta[pou_type]}\n")
        for pou in group:
            md.append(f"#### `{pou.name}`\n")
            md.append(f"- **File:** `{pou.file_path}` | **Type:** `{pou.pou_type}` | **Variables:** {len(pou.variables)}")
            if pou.return_type: md.append(f"- **Return Type:** `{pou.return_type}`")
            md.append("")
            sections: dict[str, list[Variable]] = {}
            for v in pou.variables: sections.setdefault(v.section, []).append(v)
            for sect in ("VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR"):
                svars = sections.pop(sect, [])
                if svars: md.append(f"**{sect}:**\n"); md.append(_var_table(svars))
            for sect, svars in sections.items():
                md.append(f"**{sect}:**\n"); md.append(_var_table(svars))

    md.append("---\n")
    md.append("## 🌐 GVLs\n")
    if not project.gvls: md.append("_No GVLs found._\n")
    for gvl in sorted(project.gvls, key=lambda g: g.name):
        md.append(f"### `{gvl.name}`\n")
        md.append(f"- **File:** `{gvl.file_path}` | **Variables:** {len(gvl.variables)}\n")
        md.append(_var_table(gvl.variables))

    md.append("---\n")
    md.append("## 🗂️ DUTs\n")
    if not project.duts: md.append("_No DUTs found._\n")
    duts_by_type: dict[str, list[DUT]] = {}
    for dut in sorted(project.duts, key=lambda d: (d.dut_type, d.name)):
        duts_by_type.setdefault(dut.dut_type, []).append(dut)
    for dut_type in ("STRUCT", "ENUM", "UNION", "UNKNOWN"):
        group = duts_by_type.get(dut_type, [])
        if not group: continue
        md.append(f"### {dut_type}s\n")
        for dut in group:
            md.append(f"#### `{dut.name}`\n")
            md.append(f"- **File:** `{dut.file_path}` | **Members:** {len(dut.members)}\n")
            md.append(_var_table(dut.members))

    md.append("---\n")
    md.append("## 📚 Libraries\n")
    if not project.libraries: md.append("_No library references found._\n")
    else:
        md.append("| Library | Version | Namespace | Source File |")
        md.append("|---------|---------|-----------|-------------|")
        for lib in sorted(project.libraries, key=lambda l: l.name.lower()):
            md.append(f"| `{_md_escape(lib.name)}` | `{_md_escape(lib.version)}` | `{_md_escape(lib.namespace or '—')}` | `{_md_escape(lib.source_file)}` |")
        md.append("")

    md.append("---\n")
    md.append("## ⏱️ Tasks\n")
    if not project.tasks: md.append("_No tasks found._\n")
    for task in sorted(project.tasks, key=lambda t: t.name):
        md.append(f"### `{task.name}`\n")
        if task.cycle_time: md.append(f"- **Cycle Time:** `{task.cycle_time}` µs")
        if task.priority: md.append(f"- **Priority:** `{task.priority}`")
        if task.pous:
            md.append(f"- **POUs:** " + ", ".join(f"`{p}`" for p in task.pous))
        md.append("")

    md.append("---\n")
    md.append("## 🔄 Axes\n")
    if not project.axes: md.append("_No axes found._\n")
    else:
        md.append("| Axis | Type | Source |")
        md.append("|------|------|--------|")
        for ax in sorted(project.axes, key=lambda a: a.name.lower()):
            md.append(f"| `{_md_escape(ax.name)}` | `{_md_escape(ax.axis_type or '—')}` | `{_md_escape(ax.source_file)}` |")
        md.append("")

    if project.parse_errors:
        md.append("---\n")
        md.append("## ⚠️ Parse Errors\n")
        for err in project.parse_errors: md.append(f"- {_md_escape(err)}")
        md.append("")

    md.append("---\n")
    md.append(f"*Auto-generated · Project: `{project.project_name}` · {now} · KB: `{KB_FOLDER}`*\n")
    return "\n".join(md)

# =============================================================================
# STEP 4 – UPLOAD TO COMPANYGPT
# =============================================================================

def upload_to_companygpt(content: str, filename: str, api_url: str, api_key: str, kb_folder: str) -> bool:
    if not api_key:
        log.error("COMPANYGPT_API_KEY is empty – cannot upload.")
        return False
    if not api_url:
        log.error("COMPANYGPT_API_URL is empty – cannot upload. Set it as a GitHub Variable.")
        return False

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    file_bytes = content.encode("utf-8")

    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        log.info("Upload attempt %d/%d → %s", attempt, UPLOAD_MAX_RETRIES, api_url)
        try:
            response = requests.post(
                api_url, headers=headers,
                files={"file": (filename, file_bytes, "text/markdown")},
                data={"folder": kb_folder, "overwrite": "true"},
                timeout=30,
            )
            status = response.status_code
            if status in (200, 201):
                log.info("✅ Upload successful (HTTP %d)", status)
                return True
            if status == 401:
                log.error("HTTP 401 – Check COMPANYGPT_API_KEY."); return False
            if status == 403:
                log.error("HTTP 403 – Permission denied for folder '%s'.", kb_folder); return False
            if status in (429, 503, 504):
                log.warning("HTTP %d – Retrying in %ds…", status, UPLOAD_RETRY_DELAY)
                time.sleep(UPLOAD_RETRY_DELAY); continue
            log.warning("HTTP %d: %s", status, response.text[:300])
            if attempt < UPLOAD_MAX_RETRIES: time.sleep(UPLOAD_RETRY_DELAY)
            else: log.error("All upload attempts exhausted."); return False
        except requests.exceptions.Timeout:
            log.warning("Timeout on attempt %d/%d", attempt, UPLOAD_MAX_RETRIES)
            if attempt < UPLOAD_MAX_RETRIES: time.sleep(UPLOAD_RETRY_DELAY)
            else: log.error("Upload failed: all attempts timed out."); return False
        except requests.exceptions.RequestException as exc:
            log.error("Request error: %s", exc); return False
    return False

# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    log.info("=" * 60)
    log.info("  parse_twincat.py  –  TwinCAT → CompanyGPT KB")
    log.info("=" * 60)
    log.info("Repo root : %s", REPO_ROOT)
    log.info("API URL   : %s", COMPANYGPT_API_URL or "(NOT SET)")
    log.info("API Key   : %s", "SET ✅" if COMPANYGPT_API_KEY else "NOT SET ⚠️")

    log.info("\n[STEP 1] Discovering TwinCAT files…")
    files = find_twincat_files(REPO_ROOT)
    if sum(len(v) for v in files.values()) == 0:
        log.error("No TwinCAT files found. Check repository structure.")
        return 1

    log.info("\n[STEP 2] Parsing…")
    project = parse_all(files, REPO_ROOT)

    log.info("\n[STEP 3] Generating Markdown…")
    markdown = generate_markdown(project)
    output_path = REPO_ROOT / OUTPUT_FILENAME
    try:
        output_path.write_text(markdown, encoding="utf-8")
        log.info("Written: %s (%d bytes)", output_path, len(markdown.encode("utf-8")))
    except OSError as exc:
        log.error("Cannot write output: %s", exc); return 1

    log.info("\n[STEP 4] Uploading to CompanyGPT…")
    if not COMPANYGPT_API_KEY:
        log.warning("API Key not set – skipping upload (OK for local test).")
        return 0

    success = upload_to_companygpt(markdown, OUTPUT_FILENAME, COMPANYGPT_API_URL,
                                    COMPANYGPT_API_KEY, KB_FOLDER)
    if not success:
        log.error("Upload failed."); return 1

    log.info("\n✅ Completed successfully.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
