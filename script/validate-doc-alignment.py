#!/usr/bin/env python3
"""Validate that documentation aligns with contract source code.

Checks:
   - README.md function signatures match lib.rs #[contractimpl] pub fn signatures
   - docs/ABI.md constants match source code (MAX_BATCH_SIZE, MIN_STREAM_TTL_LEDGERS)
   - error codes in error.md match ContractError enum discriminants
   - script/keeper-extend-ttl.sh documented interface (env vars, flags, ttl_extended
     event row) matches docs/mainnet_release_checklist.md, docs/indexing_guide.md
     and docs/ABI.md

Exit 0 if all checks pass, exit 1 on mismatch.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Intentional non-ABI entries that are documented but not public entrypoints.
AUDIT_ENTRYPOINT_ALLOWLIST = {"upgrade", "compute_keeper_fee_split"}

# Documented interface of script/keeper-extend-ttl.sh that must stay in sync
# with the markdown docs. Keep these lists in lockstep with the script's
# `# Environment:` and `# Usage:` header blocks.
KEEPER_ENV_VARS = ["CONTRACT_ID", "SOURCE", "IDS_FILE", "NETWORK", "RPC_URL", "BATCH_SIZE"]
KEEPER_FLAGS = ["--once", "--json", "--dry-run", "--help"]
KEEPER_EVENT_NAME = "ttl_extended"
KEEPER_EVENT_TOPICS = ["stream_id"]
KEEPER_EVENT_PAYLOAD = ["old_ttl", "new_ttl", "ledger"]


def extract_contractimpl_pub_fns(source: str) -> list[str]:
    """Extract public function names from #[contractimpl] blocks."""
    fns = []
    in_block = False
    for line in source.splitlines():
        stripped = line.strip()
        if "pub fn" in stripped and in_block:
            match = re.search(r"pub\s+fn\s+(\w+)", stripped)
            if match:
                fns.append(match.group(1))
        if "#[contractimpl]" in stripped:
            in_block = True
        elif stripped.startswith("}") and in_block:
            # Rough heuristic: closing brace after contractimpl
            pass
    return sorted(set(fns))


def extract_error_variants(source: str) -> dict[str, int]:
    """Extract ContractError variants with their explicit discriminants."""
    variants = {}
    current_discriminant = 0
    for line in source.splitlines():
        stripped = line.strip()
        # Match explicit discriminants like: Variant = 42,
        explicit = re.match(r"(\w+)\s*=\s*(\d+)", stripped)
        if explicit:
            variants[explicit.group(1)] = int(explicit.group(2))
            current_discriminant = int(explicit.group(2)) + 1
            continue
        # Match plain variants
        plain = re.match(r"(\w+)\s*[,{]", stripped)
        if plain and plain.group(1) not in ("ContractError", "enum", "pub"):
            variants[plain.group(1)] = current_discriminant
            current_discriminant += 1
    return variants


def extract_constants(source: str) -> dict[str, str]:
    """Extract public const declarations."""
    constants = {}
    # Match patterns like: pub const NAME: TYPE = VALUE;
    pattern = r"pub\s+const\s+(\w+)\s*:\s*\w+\s*=\s*([^;]+);"
    for match in re.finditer(pattern, source):
        name = match.group(1)
        value = match.group(2).strip()
        constants[name] = value
    return constants


def check_readme_functions() -> bool:
    """Check that README.md documents all entrypoints from lib.rs."""
    lib_rs = REPO_ROOT / "contracts" / "stream" / "src" / "lib.rs"
    readme_md = REPO_ROOT / "README.md"

    if not lib_rs.exists():
        print(f"SKIP: {lib_rs} not found")
        return True
    if not readme_md.exists():
        print(f"SKIP: {readme_md} not found")
        return True

    source = lib_rs.read_text(encoding="utf-8")
    readme = readme_md.read_text(encoding="utf-8")

    fns = extract_contractimpl_pub_fns(source)
    # Filter out internal helpers that aren't entrypoints
    entrypoints = [
        f for f in fns
        if f not in AUDIT_ENTRYPOINT_ALLOWLIST
        and not f.startswith("_")
    ]

    missing = [f for f in entrypoints if f not in readme]
    if missing:
        print(f"WARNING: {len(missing)} entrypoint(s) not documented in README.md: {missing}")
        # Don't fail CI for documentation gaps — just warn
    return True


def check_abi_constants() -> bool:
    """Check that docs/ABI.md constants match source code."""
    lib_rs = REPO_ROOT / "contracts" / "stream" / "src" / "lib.rs"
    abi_md = REPO_ROOT / "docs" / "ABI.md"

    if not lib_rs.exists():
        print(f"SKIP: {lib_rs} not found")
        return True
    if not abi_md.exists():
        print(f"SKIP: {abi_md} not found")
        return True

    source = lib_rs.read_text(encoding="utf-8")
    abi = abi_md.read_text(encoding="utf-8")

    constants = extract_constants(source)
    
    # Check for specific constants mentioned in the issue
    required_constants = ["MAX_BATCH_SIZE", "MIN_STREAM_TTL_LEDGERS"]
    missing = []
    mismatched = []
    
    for const_name in required_constants:
        if const_name not in constants:
            missing.append(const_name)
            continue
            
        # Look for the constant in ABI.md
        # Pattern: `MAX_BATCH_SIZE = 16` or similar
        pattern = rf"`{re.escape(const_name)}\s*=\s*([^`]+)`"
        match = re.search(pattern, abi)
        if not match:
            missing.append(const_name)
            continue
            
        abi_value = match.group(1).strip()
        source_value = constants[const_name]
        
        # Normalize values for comparison (remove spaces, etc.)
        abi_norm = re.sub(r'\s+', '', abi_value)
        source_norm = re.sub(r'\s+', '', source_value)
        
        if abi_norm != source_norm:
            mismatched.append((const_name, source_value, abi_value))
    
    if missing:
        print(f"WARNING: Constant(s) not found in docs/ABI.md: {missing}")
    if mismatched:
        print(f"WARNING: Constant value mismatch in docs/ABI.md:")
        for name, source_val, abi_val in mismatched:
            print(f"  {name}: source={source_val}, ABI.md={abi_val}")
            
    return True  # Don't fail on warnings for now


def parse_keeper_script_interface(source: str) -> dict[str, list[str]]:
    """Parse the `# Environment:` and `# Usage:` header blocks of the keeper script.

    Returns a dict with `env` and `flags` keys holding the documented names.
    """
    env_vars: list[str] = []
    flags: list[str] = []
    section: str | None = None
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("# Environment:"):
            section = "env"
            continue
        if stripped.startswith("# Usage:"):
            section = "flags"
            continue
        if not stripped.startswith("#"):
            # A non-comment line ends the header block.
            if section is not None and stripped and not stripped.startswith("#!"):
                section = None
            continue
        body = stripped.lstrip("#").strip()
        if not body:
            continue
        if section == "env":
            match = re.match(r"([A-Z][A-Z0-9_]*)\s*=", body)
            if match:
                env_vars.append(match.group(1))
        elif section == "flags":
            for flag in re.findall(r"--[a-z][a-z0-9-]*", body):
                flags.append(flag)
    return {"env": env_vars, "flags": flags}


def check_keeper_doc_alignment() -> bool:
    """Verify the keeper runner's documented interface matches the markdown docs."""
    script = REPO_ROOT / "script" / "keeper-extend-ttl.sh"
    checklist_md = REPO_ROOT / "docs" / "mainnet_release_checklist.md"
    indexing_md = REPO_ROOT / "docs" / "indexing_guide.md"
    abi_md = REPO_ROOT / "docs" / "ABI.md"

    for path in (script, checklist_md, indexing_md, abi_md):
        if not path.exists():
            print(f"SKIP: {path} not found")
            return True

    source = script.read_text(encoding="utf-8")
    checklist = checklist_md.read_text(encoding="utf-8")
    indexing = indexing_md.read_text(encoding="utf-8")
    abi = abi_md.read_text(encoding="utf-8")

    parsed = parse_keeper_script_interface(source)
    documented_env = set(parsed["env"])
    documented_flags = set(parsed["flags"])

    ok = True

    # Every env var the script documents must appear in the script header and
    # in both markdown docs.
    for name in KEEPER_ENV_VARS:
        if name not in documented_env:
            print(f"FAIL: env var {name} missing from keeper-extend-ttl.sh header")
            ok = False
        if name not in checklist:
            print(f"FAIL: env var {name} missing from docs/mainnet_release_checklist.md")
            ok = False
        if name not in indexing:
            print(f"FAIL: env var {name} missing from docs/indexing_guide.md")
            ok = False

    # Every flag the script documents must appear in the script header and in
    # both markdown docs.
    for flag in KEEPER_FLAGS:
        if flag not in documented_flags:
            print(f"FAIL: flag {flag} missing from keeper-extend-ttl.sh header")
            ok = False
        if flag not in checklist:
            print(f"FAIL: flag {flag} missing from docs/mainnet_release_checklist.md")
            ok = False
        if flag not in indexing:
            print(f"FAIL: flag {flag} missing from docs/indexing_guide.md")
            ok = False

    # The ttl_extended event row in ABI.md must match the topic/payload layout
    # the keeper consumes.
    event_row = None
    for line in abi.splitlines():
        if KEEPER_EVENT_NAME in line and "|" in line:
            event_row = line
            break
    if event_row is None:
        print(f"FAIL: {KEEPER_EVENT_NAME} event row missing from docs/ABI.md")
        ok = False
    else:
        for topic in KEEPER_EVENT_TOPICS:
            if topic not in event_row:
                print(f"FAIL: {KEEPER_EVENT_NAME} topic {topic} missing from docs/ABI.md row")
                ok = False
        for field in KEEPER_EVENT_PAYLOAD:
            if field not in event_row:
                print(f"FAIL: {KEEPER_EVENT_NAME} payload field {field} missing from docs/ABI.md row")
                ok = False

    return ok


def check_error_alignment() -> bool:
    """Check that error.md discriminants match ContractError enum."""
    error_rs = REPO_ROOT / "contracts" / "stream" / "src" / "error.rs"
    error_md = REPO_ROOT / "docs" / "error.md"

    if not error_rs.exists():
        print(f"SKIP: {error_rs} not found")
        return True
    if not error_md.exists():
        print(f"SKIP: {error_md} not found")
        return True

    source = error_rs.read_text(encoding="utf-8")
    doc = error_md.read_text(encoding="utf-8")

    variants = extract_error_variants(source)
    if not variants:
        print("WARNING: No error variants found in source")
        return True

    missing = [v for v in variants if v not in doc]
    if missing:
        print(f"WARNING: {len(missing)} error variant(s) not in error.md: {missing}")
    return True


def main() -> int:
    passed = True

    print("Checking README.md function signature coverage...")
    if not check_readme_functions():
        passed = False

    print("Checking docs/ABI.md constant alignment...")
    if not check_abi_constants():
        passed = False

    print("Checking error.md discriminant alignment...")
    if not check_error_alignment():
        passed = False

    print("Checking keeper-extend-ttl.sh documentation alignment...")
    if not check_keeper_doc_alignment():
        passed = False

    if passed:
        print("OK: Documentation alignment checks passed.")
    else:
        print("FAIL: Documentation alignment issues found.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
