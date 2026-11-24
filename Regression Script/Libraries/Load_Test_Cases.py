#!/usr/bin/env python3
"""
Load_Test_Cases.py
==================
Loads a test case JSON file into the DataRegressionSet collection: one
document per test case, inserting new ones, updating changed ones and skipping
unchanged ones. See Libraries/TestCase_Loader.py for the exact rules.

Test_Case_Generator.py already loads every test case it generates. Use this
script to load a file again after correcting the test cases that were marked
"NOT LOADED" under "_LoadStatus". The file is updated with the new status of
every test case.

Lives in Libraries\\ next to TestCase_Loader.py. Run it from the Script folder:

    python ..\\Libraries\\Load_Test_Cases.py <file.json>
    python ..\\Libraries\\Load_Test_Cases.py <file.json> --preview
    python ..\\Libraries\\Load_Test_Cases.py <file.json> -m MCPDW     (force the module name)
"""
import argparse
import json
import os
import sys

# Project root = the folder above Libraries\\ (works from Libraries\\ or Script\\)
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from Libraries.TestCase_Loader import (upsert_testcases, annotate_testcases,  # noqa: E402
                                       print_report)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="Load_Test_Cases.py",
        description="Insert / update / skip test cases in DataRegressionSet "
                    "(one document per test case, no duplicates on re-runs). "
                    "Incomplete or invalid test cases are not loaded; the "
                    "reason is written into the file under _LoadStatus.")
    p.add_argument("file", help="Test case JSON file (an array of test cases)")
    p.add_argument("-m", "--module", help="Set this Module on every test case in the file")
    p.add_argument("-e", "--environment",
                   help="MongoDB environment holding DataRegressionSet "
                        "(default: [REGRESSION_DATA] test_data_environment)")
    p.add_argument("--preview", action="store_true",
                   help="Show what would be inserted/updated/skipped; write nothing")
    p.add_argument("--overwrite-manual", action="store_true",
                   help="Allow updates to hand-written test cases (normally never changed)")
    args = p.parse_args(argv)

    try:
        with open(args.file, encoding="utf-8") as fh:
            testcases = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    single = isinstance(testcases, dict)
    if single:
        testcases = [testcases]
    if not isinstance(testcases, list):
        print("ERROR: the file must contain a JSON array of test cases.", file=sys.stderr)
        return 2
    if args.module:
        for tc in testcases:
            if isinstance(tc, dict):
                tc["Module"] = args.module

    try:
        results = upsert_testcases(testcases, environment=args.environment,
                                   preview=args.preview,
                                   overwrite_manual=args.overwrite_manual)
    except Exception as exc:
        print(f"ERROR: could not reach DataRegressionSet — nothing loaded: {exc}",
              file=sys.stderr)
        return 3

    if not args.preview:
        annotate_testcases(testcases, results)
        try:
            with open(args.file, "w", encoding="utf-8") as fh:
                json.dump(testcases[0] if single else testcases, fh, indent=2, default=str)
        except OSError as exc:
            print(f"WARNING: loaded, but could not update {args.file}: {exc}", file=sys.stderr)

    print_report(results, preview=args.preview, json_path=args.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())