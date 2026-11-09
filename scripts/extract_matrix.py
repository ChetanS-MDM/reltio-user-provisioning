#!/usr/bin/env python3
"""Extract the EPH security roles matrix (Excel) into clean JSON.

Reads the 'Functionality-Attributes' sheet (use case -> roles) and the
'Use Case' sheet (retired status), normalizes the messy role cells (NBSP, tabs,
run-on spacing, comma separation), and splits Reltio roles from ECDP roles.

Usage:
    python extract_matrix.py EPH_Security_Roles_Matrix_v108.xlsx roles_matrix.json
"""
import json
import re
import sys

import openpyxl

ROLE_RE = re.compile(r"^(ROLE_|ECDP_|ACCEPT_CHANGE_REQUEST)")


def _norm(name) -> str:
    return re.sub(r"\s+", " ", str(name)).strip()


def _parse_roles(raw):
    """Return (reltio_roles, ecdp_roles), order-preserving and de-duplicated.

    Separators in the source cells are newlines, tabs, commas, and run-on
    spaces. We deliberately do NOT split on a single space, because country
    placeholders like 'ROLE_LILLY_EPH_<2 DIGIT Country Code or GLOBAL>_X'
    contain single spaces and must stay whole.
    """
    if not raw:
        return [], []
    raw = raw.replace("\xa0", " ")
    reltio, ecdp, seen = [], [], set()
    for tok in re.split(r"[\n\r\t,]+|\s{2,}", raw):
        tok = tok.strip()
        if not tok or not ROLE_RE.match(tok) or tok in seen:
            continue
        seen.add(tok)
        (ecdp if tok.startswith("ECDP_") else reltio).append(tok)
    return reltio, ecdp


def extract(xlsx_path: str, source_version: str = "") -> dict:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    fa = wb["Functionality-Attributes"]

    # retired flags from the Use Case sheet
    uc_status = {}
    ucs = wb["Use Case"]
    for r in range(2, ucs.max_row + 1):
        name, st = ucs.cell(r, 1).value, ucs.cell(r, 3).value
        if name:
            uc_status[_norm(name).lower()] = st

    use_cases = {}
    for r in range(2, fa.max_row + 1):
        uc = fa.cell(r, 1).value
        roles_raw = fa.cell(r, 2).value
        notes = fa.cell(r, 3).value
        if not uc:
            continue
        uc_clean = _norm(uc)
        if uc_clean.startswith("*"):          # the footnote blob, not a use case
            continue
        reltio, ecdp = _parse_roles(roles_raw)
        if not reltio and not ecdp:
            continue
        retired = (str(notes).strip().lower() == "retired") or (
            uc_status.get(uc_clean.lower()) == "Retired"
        )
        use_cases[uc_clean] = {
            "reltio_roles": reltio,
            "ecdp_roles": ecdp,
            "retired": bool(retired),
            "notes": notes if isinstance(notes, str) else None,
        }

    if not source_version:
        m = re.search(r"[_-]v(\d+)", xlsx_path, re.IGNORECASE)
        source_version = f"v{m.group(1)}" if m else "unknown"

    return {"source_version": source_version, "use_cases": use_cases}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    data = extract(sys.argv[1])
    with open(sys.argv[2], "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {sys.argv[2]} | version={data['source_version']} "
          f"| use_cases={len(data['use_cases'])}")


if __name__ == "__main__":
    main()
