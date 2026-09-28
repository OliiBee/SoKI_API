#!/usr/bin/env python3
"""
parse_twincat.py
Parses TwinCAT 3 project files and extracts structured KB content.
Categories: tasks, dut, gvl, libs, motion, pous
Output: JSON file ready for CompanyGPT KB upload
"""

import os
import json
import argparse
import re
from pathlib import Path
from xml.etree import ElementTree as ET


# ── Namespace used in TwinCAT XML files ────────────────────────────────────
NS = {"tc": "http://www.beckhoff.com/schemas/2009/PlcOpen"}


# ═══════════════════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════════════════

def find_files(root: Path, extensions: list[str]) -> list[Path]:
    """Recursively find all files with given extensions."""
    result = []
    for ext in extensions:
        result.extend(root.rglob(f"*{ext}"))
    return sorted(result)


def safe_text(element, tag: str, ns: dict = None) -> str:
    """Safely extract text from an XML child element."""
    if ns:
        child = element.find(tag, ns)
    else:
        child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def clean_declaration(text: str) -> str:
    """Remove excessive blank lines from ST declaration blocks."""
    lines = [l.rstrip() for l in text.splitlines()]
    cleaned = []
    prev_blank = False
    for line in lines:
        if line == "":
            if not prev_blank:
                cleaned.append(line)
            prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False
    return "\n".join(cleaned).strip()


def is_motion_pou(name: str, declaration: str) -> bool:
    """Detect Motion-related POUs by name prefix or MC_ usage."""
    motion_patterns = [
        r"\bMC_\w+",
        r"\bAxis\b",
        r"\bDrive\b",
        r"\bMotion\b",
        r"MC_Power",
        r"MC_MoveAbsolute",
        r"MC_Home",
    ]
    if name.upper().startswith(("MC_", "MOTION", "AXIS", "DRIVE")):
        return True
    for pattern in motion_patterns:
        if re.search(pattern, declaration, re.IGNORECASE):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# PARSER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def parse_pou_file(filepath: Path) -> dict | None:
    """
    Parse a .TcPOU file.
    Returns dict with name, pou_type, declaration, implementation, is_motion.
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        # Strip namespace for simpler search
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        pou = root.find(".//POU")
        if pou is None:
            return None

        name        = pou.get("Name", filepath.stem)
        pou_type    = pou.get("pouType", "FUNCTION_BLOCK")
        declaration = safe_text(pou, "Declaration")
        impl_elem   = pou.find("Implementation")
        implementation = ""
        if impl_elem is not None:
            st_elem = impl_elem.find("ST")
            if st_elem is not None and st_elem.text:
                implementation = st_elem.text.strip()

        return {
            "name":           name,
            "pou_type":       pou_type,
            "file":           str(filepath.name),
            "declaration":    clean_declaration(declaration),
            "implementation": implementation[:2000],  # Limit for KB
            "is_motion":      is_motion_pou(name, declaration),
            "line_count":     len(implementation.splitlines()),
        }

    except ET.ParseError as e:
        print(f"  ⚠️  XML Parse Error in {filepath.name}: {e}")
        return None


def parse_dut_file(filepath: Path) -> dict | None:
    """
    Parse a .TcDUT file (Struct, Enum, Union, Alias).
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        dut = root.find(".//DUT")
        if dut is None:
            return None

        name        = dut.get("Name", filepath.stem)
        declaration = safe_text(dut, "Declaration")

        # Detect DUT type from declaration
        dut_type = "STRUCT"
        decl_upper = declaration.upper()
        if "TYPE" in decl_upper and "ENUM" in decl_upper:
            dut_type = "ENUM"
        elif "UNION" in decl_upper:
            dut_type = "UNION"
        elif "ALIAS" in decl_upper:
            dut_type = "ALIAS"

        return {
            "name":        name,
            "dut_type":    dut_type,
            "file":        str(filepath.name),
            "declaration": clean_declaration(declaration),
        }

    except ET.ParseError as e:
        print(f"  ⚠️  XML Parse Error in {filepath.name}: {e}")
        return None


def parse_gvl_file(filepath: Path) -> dict | None:
    """
    Parse a .TcGVL file (Global Variable List).
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        gvl = root.find(".//GVL")
        if gvl is None:
            return None

        name = gvl.get("Name", filepath.stem)
        declarations = safe_text(gvl, "Declarations")

        return {
            "name":         name,
            "file":         str(filepath.name),
            "declarations": clean_declaration(declarations),
        }

    except ET.ParseError as e:
        print(f"  ⚠️  XML Parse Error in {filepath.name}: {e}")
        return None


def parse_task_file(filepath: Path) -> dict | None:
    """
    Parse a .TcTask file (Task configuration).
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        task_elem = root.find(".//Task")
        if task_elem is None:
            task_elem = root  # Some task files use root directly

        name        = task_elem.get("Name", filepath.stem)
        priority    = task_elem.get("Priority", "?")
        cycle_time  = task_elem.get("CycleTime", "?")

        # Collect POUs called in this task
        pou_calls = []
        for po in task_elem.findall(".//POUCall"):
            pou_name = po.get("Name") or po.get("ObjId", "")
            if pou_name:
                pou_calls.append(pou_name)

        return {
            "name":       name,
            "file":       str(filepath.name),
            "priority":   priority,
            "cycle_time": cycle_time,
            "pou_calls":  pou_calls,
        }

    except ET.ParseError as e:
        print(f"  ⚠️  XML Parse Error in {filepath.name}: {e}")
        return None


def parse_tsproj_for_libs(filepath: Path) -> list[dict]:
    """
    Parse .tsproj to extract library references.
    """
    libs = []
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        # Standard library references
        for ref in root.findall(".//LibraryReference"):
            lib_name    = ref.get("Include", "")
            namespace   = ref.get("Namespace", "")
            version     = ref.get("Version", "")
            if lib_name:
                libs.append({
                    "name":      lib_name,
                    "namespace": namespace,
                    "version":   version,
                    "source":    str(filepath.name),
                })

        # Placeholder references (e.g. Tc2_MC2)
        for ref in root.findall(".//PlaceholderReference"):
            lib_name  = ref.get("Key", "")
            resolved  = safe_text(ref, "Default")
            if lib_name:
                libs.append({
                    "name":     lib_name,
                    "resolved": resolved,
                    "type":     "placeholder",
                    "source":   str(filepath.name),
                })

    except ET.ParseError as e:
        print(f"  ⚠️  XML Parse Error in {filepath.name}: {e}")

    return libs


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════

def build_payload(repo_root: Path, categories: list[str]) -> dict:
    """
    Walk repo, parse all TwinCAT files, return structured payload dict.
    """
    payload = {}

    print(f"\n📂 Scanning: {repo_root}")

    # ── POUs ────────────────────────────────────────────────────────────────
    if "pous" in categories or "motion" in categories:
        pou_files = find_files(repo_root, [".TcPOU"])
        print(f"\n🔷 TcPOU files found: {len(pou_files)}")

        all_pous    = []
        motion_pous = []

        for f in pou_files:
            result = parse_pou_file(f)
            if result:
                if result["is_motion"]:
                    motion_pous.append(result)
                else:
                    all_pous.append(result)
                print(f"  ✅ {result['name']} [{result['pou_type']}]"
                      + (" 🔵 MOTION" if result["is_motion"] else ""))

        if "pous" in categories:
            payload["pous"] = all_pous
        if "motion" in categories:
            payload["motion"] = motion_pous

    # ── DUTs ────────────────────────────────────────────────────────────────
    if "dut" in categories:
        dut_files = find_files(repo_root, [".TcDUT"])
        print(f"\n🔷 TcDUT files found: {len(dut_files)}")
        duts = []
        for f in dut_files:
            result = parse_dut_file(f)
            if result:
                duts.append(result)
                print(f"  ✅ {result['name']} [{result['dut_type']}]")
        payload["dut"] = duts

    # ── GVLs ────────────────────────────────────────────────────────────────
    if "gvl" in categories:
        gvl_files = find_files(repo_root, [".TcGVL"])
        print(f"\n🔷 TcGVL files found: {len(gvl_files)}")
        gvls = []
        for f in gvl_files:
            result = parse_gvl_file(f)
            if result:
                gvls.append(result)
                print(f"  ✅ {result['name']}")
        payload["gvl"] = gvls

    # ── Tasks ───────────────────────────────────────────────────────────────
    if "tasks" in categories:
        task_files = find_files(repo_root, [".TcTask"])
        print(f"\n🔷 TcTask files found: {len(task_files)}")
        tasks = []
        for f in task_files:
            result = parse_task_file(f)
            if result:
                tasks.append(result)
                print(f"  ✅ {result['name']} "
                      f"[Prio: {result['priority']}, Cycle: {result['cycle_time']}]")
        payload["tasks"] = tasks

    # ── Libraries (from .tsproj) ─────────────────────────────────────────
    if "libs" in categories:
        tsproj_files = find_files(repo_root, [".tsproj"])
        print(f"\n🔷 .tsproj files found: {len(tsproj_files)}")
        all_libs = []
        for f in tsproj_files:
            libs = parse_tsproj_for_libs(f)
            all_libs.extend(libs)
            print(f"  ✅ {f.name}: {len(libs)} library references")
        payload["libs"] = all_libs

    return payload


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Parse TwinCAT 3 project files for CompanyGPT KB"
    )
    parser.add_argument(
        "--categories",
        default="tasks,dut,gvl,libs,motion,pous",
        help="Comma-separated list of categories to parse"
    )
    parser.add_argument(
        "--output",
        default="twincat_kb_payload.json",
        help="Output JSON file path"
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repository root path (default: REPO_ROOT env or current dir)"
    )
    args = parser.parse_args()

    # Determine repo root
    repo_root = Path(
        args.root
        or os.environ.get("REPO_ROOT", ".")
    ).resolve()

    categories = [c.strip().lower() for c in args.categories.split(",")]
    print(f"🎯 Categories: {categories}")
    print(f"📁 Repo root:  {repo_root}")

    payload = build_payload(repo_root, categories)

    # Write output
    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Summary
    print(f"\n{'═'*50}")
    print(f"✅ Output written: {output_path}")
    for cat, items in payload.items():
        count = len(items) if isinstance(items, list) else 0
        print(f"   {cat:10s}: {count:3d} Einträge")
    print(f"{'═'*50}\n")


if __name__ == "__main__":
    main()
