#!/usr/bin/env python3
"""
Interactive script to review and rebase+merge Wokwi PRs
on a TinyTapeout shuttle repository.
"""

import argparse
import base64
import json
import os
import re
import subprocess

import yaml

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"


def gh(*args, token=None, json_output=False):
    cmd = ["gh"] + list(args)
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        print(f"  gh error: {result.stderr.strip()}")
        return None
    if json_output:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"  gh error: invalid JSON response")
            return None
    return result.stdout.strip()


def get_file_content(repo, path, ref, token=None):
    """Fetch a file from a specific branch via the GitHub API."""
    data = gh(
        "api",
        f"repos/{repo}/contents/{path}?ref={ref}",
        "--jq", ".content",
        token=token,
    )
    if not data:
        return None
    try:
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Warning: failed to decode {path}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Review and rebase+merge Wokwi PRs")
    parser.add_argument("repo", help="GitHub repository (e.g. TinyTapeout/tinytapeout-sky-26a)")
    parser.add_argument("--token", "-t", help="GitHub token (or set GH_TOKEN / GITHUB_TOKEN env var)")
    args = parser.parse_args()

    repo = args.repo
    token = args.token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Warning: No GitHub token provided. Using existing gh auth.")
        print("  Pass --token TOKEN, or set GH_TOKEN / GITHUB_TOKEN env var.\n")

    print(f"Fetching open PRs from {repo}...")
    prs = gh(
        "pr", "list",
        "--repo", repo,
        "--limit", "500",
        "--json", "number,title,headRefName,body",
        token=token,
        json_output=True,
    )
    if not prs:
        print("No open PRs found.")
        return

    # Filter Wokwi PRs (branch name contains "wokwi")
    wokwi_prs = [pr for pr in prs if "wokwi" in pr["headRefName"].lower()]
    print(f"Found {len(prs)} open PRs, {len(wokwi_prs)} are Wokwi projects.\n")

    # Sort by PR number (ascending)
    wokwi_prs.sort(key=lambda pr: pr["number"])

    merged = 0
    skipped = 0

    for i, pr in enumerate(wokwi_prs, 1):
        # Clear screen and scrollback buffer
        print("\033[3J\033[2J\033[H", end="", flush=True)

        num = pr["number"]
        branch = pr["headRefName"]

        # Extract project directory from branch name (e.g. projects/tt_um_wokwi_XXX-runid -> projects/tt_um_wokwi_XXX)
        parts = branch.split("-")
        project_dir = "-".join(parts[:-1]) if len(parts) > 1 else branch

        print("=" * 70)
        print(f"[{i}/{len(wokwi_prs)}] PR #{num}: {pr['title']}")
        print(f"  Branch: {branch}")
        print(f"  {pr.get('body', '').split('\n')[0]}")
        print()

        # Check CI status
        checks = gh(
            "pr", "checks", str(num),
            "--repo", repo,
            "--json", "name,state",
            token=token,
            json_output=True,
        )
        if checks:
            all_success = all(c["state"] == "SUCCESS" for c in checks)
            if all_success:
                print(f"  {GREEN}Checks: all passed{RESET}")
            else:
                for c in checks:
                    if c["state"] != "SUCCESS":
                        print(f"  {RED}WARNING: {c['name']} -> {c['state']}{RESET}")
        else:
            print(f"  {RED}WARNING: Could not fetch checks{RESET}")
        print()

        # Fetch and display key fields from info.yaml
        info_yaml_raw = get_file_content(repo, f"{project_dir}/info.yaml", branch, token=token)
        if info_yaml_raw:
            try:
                info = yaml.safe_load(info_yaml_raw)
                proj = info.get("project", {})
                pinout = info.get("pinout", {})

                print("--- info.yaml ---")
                print(f"  Title:       {proj.get('title', '?')}")
                print(f"  Author:      {proj.get('author', '?')}")
                print(f"  Discord:     {proj.get('discord', '') or '(none)'}")
                print(f"  Description: {proj.get('description', '?')}")

                # Condensed pinout: group by type (ui, uo, uio), one line each
                used = {k: v for k, v in pinout.items() if v and v.strip() and v.strip() != '""'}
                if used:
                    groups = {"ui": [], "uo": [], "uio": []}
                    for k, v in used.items():
                        for prefix in ("uio", "uo", "ui"):
                            if k.startswith(prefix):
                                groups[prefix].append(f"{k}={v}")
                                break
                    for prefix, label in [("ui", "Inputs"), ("uo", "Outputs"), ("uio", "Bidir")]:
                        if groups[prefix]:
                            print(f"  {label:10s}   {', '.join(groups[prefix])}")
                else:
                    print("  Pinout:      (all blank)")
                print()
            except yaml.YAMLError:
                print("--- info.yaml (parse error, showing raw) ---")
                print(info_yaml_raw[:2000])
                print()

        # Fetch and display info.md (strip boilerplate comment)
        info_md = get_file_content(repo, f"{project_dir}/docs/info.md", branch, token=token)
        if info_md:
            info_md = re.sub(r'<!---?\s*\n.*?-->\n*', '', info_md, flags=re.DOTALL)
        if info_md and info_md.strip():
            print("--- docs/info.md ---")
            print(info_md[:2000])
            print()

        # Show the command and ask user
        print(f"Command: gh pr merge {num} --repo {repo} --rebase")
        while True:
            answer = input(f"Rebase+merge PR #{num}? [y/n/q] ").strip().lower()
            if answer in ("y", "n", "q"):
                break
            print("  Please enter y, n, or q (quit).")

        if answer == "q":
            print("Quitting.")
            break
        elif answer == "n":
            skipped += 1
            print(f"  Skipped PR #{num}\n")
            continue

        # Merge
        print(f"  Merging PR #{num}...")
        result = gh(
            "pr", "merge", str(num),
            "--repo", repo,
            "--rebase",
            token=token,
        )
        if result is not None:
            print(f"  Merged PR #{num}")
            merged += 1
        else:
            print(f"  Failed to merge PR #{num}")
            skipped += 1
        print()

    print("=" * 70)
    print(f"Done. Merged: {merged}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
